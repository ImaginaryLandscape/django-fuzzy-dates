import calendar
import re
from datetime import date, datetime
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from timezonefinder import TimezoneFinder
from zoneinfo import ZoneInfo


# This regex matches dates in the format yyyy, yyyy.mm, or yyyy.mm.dd (other
# separators are allowed, too, e.g., yyyy-mm-dd or yyyy/mm/dd). A time value
# in the format hh:mm can also be appended. If a time is present, a timezone
# value with the format area/location must also be present. Thanks to
# https://stackoverflow.com/questions/15474741/python-regex-optional-capture-group
DATE_PATTERN = re.compile(
    r"(\d{4})"                                                # year
    r"(?:[.\-/](\d{2})"                                       # optional month
    r"(?:[.\-/](\d{2})"                                       # optional day
    r"(?:\s+(\d{2}):(\d{2})\s+([A-Za-z_]+/[A-Za-z_]+))?)?)$"  # optional time block
)

DATE_FIELD_ORDER = getattr(settings, "FUZZY_DATE_FIELD_ORDER", "mdy").lower()
DATE_FIELD_SEPARATOR = getattr(settings, "FUZZY_DATE_FIELD_SEPARATOR", "/")
DATE_FIELD_PLACEHOLDERS = {
    "y": "yyyy",
    "m": "mm",
    "d": "dd",
}
DATE_FIELD_REQUIRED = {
    "y": True,
    "m": False,
    "d": False,
}
DATE_FIELD_WIDGET_NAMES = {
    "y": "year_widget",
    "m": "month_widget",
    "d": "day_widget",
}
EMPTY_CHOICE = (("", "---------"),)
TRIM_CHAR = "0" if getattr(settings, "FUZZY_DATE_TRIM_LEADING_ZEROS", False) else ""
TZ_PATTERN = re.compile(r"^[A-Za-z]+/[A-Za-z_]+")

tf = TimezoneFinder()

if len(DATE_FIELD_ORDER) != 3 or set(DATE_FIELD_ORDER) != set("ymd"):
    raise ValueError("The FUZZY_DATE_FIELD_ORDER setting must be a 3-character string containing 'y', 'm', and 'd'.")

if DATE_FIELD_SEPARATOR not in ("-", ".", "/"):
    raise ValueError("The FUZZY_DATE_FIELD_SEPARATOR setting must be one of '-', '.', or '/'.")


# All dates are stored in the DB as strings formatted as "yyyy.mm.dd" or as
# "yyyy.mm.dd HH:MM tz". Using this format means that comparing and sorting
# dates is as easy as comparing and sorting strings. For fuzzy dates (e.g.,
# just a year or just a year and a month), we use a value of "00" in place
# of the missing month and/or day. Fuzzy dates can then be sorted with non-
# fuzzy dates.
class FuzzyDate(str):
    def __new__(cls, *args, **kwargs):
        base = ""
        year = ""
        month = ""
        day = ""
        hour = ""
        minute = ""
        tz = ""

        if args and kwargs:
            raise ValueError("Cannot mix positional and keyword arguments when creating a FuzzyDate.")

        if args:
            seed = args[0]

            if isinstance(seed, FuzzyDate):
                year = seed.year
                month = seed.month
                day = seed.day
                hour = seed.hour
                minute = seed.minute
                tz = seed.tz

            elif isinstance(seed, datetime):
                if seed.tzinfo is None:
                    raise ValueError("Datetime must be timezone-aware or 'utc'.")
                elif seed.tzinfo == datetime.timezone.utc:
                    tz_key = "UTC"
                elif not hasattr(seed.tzinfo, "key"):
                    raise ValueError("Datetime must use a named IANA zone or UTC.")
                else:
                    tz_key = seed.tzinfo.key  # e.g., 'America/Chicago'
                # else
                year = seed.year
                month = seed.month
                day = seed.day
                hour = seed.hour
                minute = seed.minute
                tz = tz_key

            elif isinstance(seed, date):
                year = seed.year
                month = seed.month
                day = seed.day

            elif isinstance(seed, str):
                if not (m := DATE_PATTERN.fullmatch(seed.strip())):
                    raise ValueError(f"Invalid FuzzyDate string: {seed}")
                # else
                year, month, day, hour, minute, tz = m.groups()

            else:
                raise TypeError(f"Unable to create FuzzyDate from type: {type(seed)}")
        elif kwargs:
            def norm(val):
                return "" if val is None else val

            year = norm(kwargs.get("y"))
            month = norm(kwargs.get("m"))
            day = norm(kwargs.get("d"))
            hour = norm(kwargs.get("hour"))
            minute = norm(kwargs.get("minute"))
            tz = norm(kwargs.get("tz"))

        # At this point all date and time values should be strings or None, regardless of how they were passed in.
        if not {year, month, day, hour, minute, tz} <= {None, ""}:
            # Some date or time element has been specified
            if not year:
                raise ValueError("Year must be specified if any other date or time component is specified")

            if day and not month:
                raise ValueError("If day is specified, month must also be specified")

            month = month or "00"
            day = day or "00"

            try:
                # Leverage the "datetime" library's "date()" function to check that values
                # are valid.  We temporarily replace any fuzzy values with 1. This lets us
                # eliminate invalid dates like 2000.13.01 or 2000.01.32.
                int_year = int(year)
                int_month = int(month) if month != "00" else 1
                int_day = int(day) if day != "00" else 1
                if int_year < 1000 or int_year > 9999:
                    # Keep the year within this range as years outside it would break
                    # sorting (e.g., "900" > "1000" alphanumerically speaking). Later
                    # on I might try to relax this restriction by padding short years
                    # with zeros, but it would take some doing.
                    raise ValueError("The year must be no less than 1000 and no greater than 9999.")
                # else
                date(year=int_year, month=int_month, day=int_day)
            except ValueError as e:
                raise e

            # Looks like we have a valid date
            base = f"{int(year):04}.{int(month):02}.{int(day):02}"

            # Now deal with the time values.
            if not {hour, minute, tz} <= {None, ""}:
                # Some time element has been specified
                if not day or not month:
                    raise ValueError("If any time fields are specified, day and month must also be specified")
                # Now we know that day, month, and year are all specified
                if {hour, minute, tz} & {None, ""}:
                    # Although we know that some time element has been specified, not all of them are
                    raise ValueError("If any of hour, minute, or timezone is specified, all must be specified")

                try:
                    hour = f"{int(hour):02}"
                except (ValueError, TypeError):
                    raise ValueError("Hour must be a valid integer")
                if not (0 <= int(hour) < 24):
                    raise ValueError("Hour must be between 0 and 23.")
                try:
                    minute = f"{int(minute):02}"
                except (ValueError, TypeError):
                    raise ValueError("Minute must be a valid integer")
                if not (0 <= int(minute) < 60):
                    raise ValueError("Minute must be between 0 and 59.")

                if not TZ_PATTERN.match(tz):
                    raise ValueError("Timezone must be in the format Area/Location (e.g., America/Chicago).")

                base += f" {hour}:{minute} {tz}"

        instance = super().__new__(cls, base)
        instance.year = year
        instance.month = month
        instance.day = day
        instance.hour = hour
        instance.minute = minute
        instance.tz = tz
        return instance

    def __repr__(self):
        return f"FuzzyDate({super().__repr__()})"

    def __str__(self):
        data_dict = dict(zip("ymd", self.as_list()))
        date_part = DATE_FIELD_SEPARATOR.join(
            [data_dict[el].lstrip(TRIM_CHAR) for el in DATE_FIELD_ORDER if data_dict[el]]
        )
        if self.hour is not None and self.minute is not None and self.tz:
            return f"{date_part} {int(self.hour):02}:{int(self.minute):02} {self.tz}"
        # else
        return date_part

    def as_list(self):
        return [self.year, self.month, self.day]

    def get_range(self):
        start_year = self.year
        start_month = self.month or "01"
        start_day = self.day or "01"
        end_year = self.year
        end_month = self.month or "12"
        end_day = self.day or str(calendar.monthrange(int(end_year), int(end_month))[1])
        return (
            FuzzyDate(y=start_year, m=start_month, d=start_day),
            FuzzyDate(y=end_year, m=end_month, d=end_day)
        )

    def get_datetime(self):
        """
        Convert this FuzzyDate instance to a timezone-aware datetime.datetime object, if possible
        """
        if any(val in (None, "") for val in [self.year, self.month, self.day, self.hour, self.minute, self.tz]):
            return None
        # else
        try:
            return datetime(
                year=int(self.year),
                month=int(self.month),
                day=int(self.day),
                hour=int(self.hour),
                minute=int(self.minute),
                tzinfo=ZoneInfo(self.tz)
            )
        except Exception as e:
            raise ValueError(f"Unable to convert FuzzyDate to datetime: {e}")
    
    @property
    def is_fuzzy(self):
        return self.day == ""


class FuzzyDateWidget(forms.MultiWidget):

    # Django is surprisingly resistant to allowing "type='time'" on an input element in
    # a multi-widget form field.  And we need that type to get the browser to display a
    # time picker. Overriding the template was the only way I could find to do it.  And
    # it still won't work if using Grappelli instead of the default admin interface.
    class CustomTimeInput(forms.TimeInput):
        template_name = "fuzzy_dates/time_widget.html"        


    def __init__(self, attrs=None):
        # Define the date-related input widgets in the user's preferred order.        
        widgets = [
            forms.NumberInput(attrs={"min": 1, "placeholder": DATE_FIELD_PLACEHOLDERS[el]})
            for el in DATE_FIELD_ORDER
        ]
        # Now add the time widgets
        widgets += [
            self.CustomTimeInput(attrs={"placeholder": "hh:mm"}),
            forms.Select(choices=EMPTY_CHOICE + tuple([(name, name) for name in sorted(tf.timezone_names)]))
        ]
        super().__init__(widgets, attrs)
        for widget_name, widget in zip(self.widgets_names, self.widgets):
            widget.attrs["data-name"] = widget_name

    def decompress(self, value):
        print("Decompress called with value:", str(value))
        if value:  # will be a FuzzyDate object
            data_dict = dict(zip("ymd", value.as_list()))
            time_str = (
                f"{int(value.hour):02}:{int(value.minute):02}"
                if value.hour is not None and value.minute is not None
                else ""
            )
            return [
                data_dict[el] for el in DATE_FIELD_ORDER   # rearrange to the user's preferred order
            ] + [time_str, value.tz or ""]
        return ["", "", "", "", ""]

    # The following properties allow the form to access the subwidgets using user-friendly names.
    # For example, a form could replace the timezone widget with a readonly text field like this:
    #
    #     def __init__(self, *args, **kwargs):
    #         super().__init__(*args, **kwargs)
    #         ...
    #         self.fields["start_date"].widget.timezone_widget = forms.TextInput(attrs={"readonly": True})
    @property
    def year_widget(self):
        return self.widgets[DATE_FIELD_ORDER.index("y")]

    @year_widget.setter
    def year_widget(self, value):
        self.widgets[DATE_FIELD_ORDER.index("y")] = value

    @property
    def month_widget(self):
        return self.widgets[DATE_FIELD_ORDER.index("m")]

    @month_widget.setter
    def month_widget(self, value):
        self.widgets[DATE_FIELD_ORDER.index("m")] = value

    @property
    def date_widget(self):
        return self.widgets[DATE_FIELD_ORDER.index("d")]

    @date_widget.setter
    def date_widget(self, value):
        self.widgets[DATE_FIELD_ORDER.index("d")] = value

    @property
    def time_widget(self):
        return self.widgets[3]

    @time_widget.setter
    def time_widget(self, value):
        self.widgets[3] = value

    @property
    def timezone_widget(self):
        return self.widgets[4]

    @timezone_widget.setter
    def timezone_widget(self, value):
        self.widgets[4] = value



class FuzzyDateFormField(forms.MultiValueField):
    def __init__(self, *args, **kwargs):
        kwargs.pop("max_length", None)  # max_length is here because FuzzyDateField (below) subclasses
                                        # models.CharField, but it's not valid for forms.MultiValueField
        fields = [
            forms.IntegerField(min_value=1, required=DATE_FIELD_REQUIRED[el])
            for el in DATE_FIELD_ORDER
        ] + [
            forms.TimeField(required=False),
            forms.CharField(required=False)
        ]
        kwargs["require_all_fields"] = False
        super().__init__(fields, *args, **kwargs)
        self.widget = FuzzyDateWidget()
        for field in fields[:3]:
            for validator in field.validators:
                if isinstance(validator, MinValueValidator):
                    validator.message = "Ensure all values are greater than 1."


    def compress(self, data_list):
        if data_list:
            date_part = dict(zip(DATE_FIELD_ORDER, data_list[:3]))
            time_obj = data_list[3]
            tz_val = data_list[4]

            if time_obj and tz_val:
                hour = f"{time_obj.hour:02}"
                minute = f"{time_obj.minute:02}"
                return FuzzyDate(**date_part, hour=hour, minute=minute, tz=tz_val)

            return FuzzyDate(**date_part)
        return ""


class FuzzyDateField(models.CharField):
    def __init__(self, *args, **kwargs):
        kwargs["max_length"] = 50
        super().__init__(*args, **kwargs)

    def formfield(self, **kwargs):
        kwargs.update({"form_class": FuzzyDateFormField})
        return super().formfield(**kwargs)

    def from_db_value(self, value, expression, connection):
        return self.to_python(value)
        #if value:
        #    # Values coming from the DB should be in the format yyyy.mm.dd with an optional time and timezone
        #    return FuzzyDate(value)
        ## else
        #return value

    def to_python(self, value):
        if isinstance(value, FuzzyDate):
            return value
        if value in self.empty_values:
            return FuzzyDate()
        try:
            return FuzzyDate(value)
        except ValueError as e:
            raise ValidationError(e)
