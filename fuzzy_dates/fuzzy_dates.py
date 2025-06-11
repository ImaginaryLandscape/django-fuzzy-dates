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


tf = TimezoneFinder()

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
EMPTY_CHOICE = (("", "---------"),)
TRIM_CHAR = "0" if getattr(settings, "FUZZY_DATE_TRIM_LEADING_ZEROS", False) else ""
TZ_PATTERN = re.compile(r"^[A-Za-z]+/[A-Za-z_]+")


if len(DATE_FIELD_ORDER) != 3 or set(DATE_FIELD_ORDER) != set("ymd"):
    raise ValueError("The FUZZY_DATE_FIELD_ORDER setting must be a 3-character string containing 'y', 'm', and 'd'.")

if DATE_FIELD_SEPARATOR not in ("-", ".", "/"):
    raise ValueError("The FUZZY_DATE_FIELD_SEPARATOR setting must be one of '-', '.', or '/'.")


# We use a custom metaclass to normalize parameters before they are passed to
# the class's "__new__()" and "__init__()" methods.  It also allows FuzzyDate
# instances to be initialized either with a string or via keyword arguments.
class CustomMeta(type):
    def __call__(cls, seed=None, *args, **kwargs):
        if seed:
            if isinstance(seed, str):
                if m := DATE_PATTERN.match(seed):
                    year, month, day, hour, minute, tz = m.groups()
                else:
                    raise ValueError("Dates must be formatted as yyyy, yyyy.mm, yyyy.mm.dd, or full timestamp with time and timezone")
            elif isinstance(seed, datetime):
                if seed.tzinfo is None:
                    raise ValueError("Datetime object must be timezone-aware")
                # else
                year, month, day = seed.year, seed.month, seed.day
                hour = f"{seed.hour:02}"
                minute = f"{seed.minute:02}"

                if hasattr(seed.tzinfo, "key"):
                    tz = seed.tzinfo.key  # e.g., 'America/Chicago'
                elif seed.tzinfo == timezone.utc:
                    tz = "UTC"
                else:
                    raise ValueError("Unsupported timezone format. Must be a zoneinfo.ZoneInfo or timezone.utc.")
            elif isinstance(seed, date):
                year, month, day = seed.year, seed.month, seed.day
                hour = None
                minute = None
                tz = None
            else:
                raise TypeError("Only a string, a date, or a datetime can be passed as an initialization argument")
        else:
            # These could be strings, ints, or None at this point
            year = kwargs.get("y")
            month = kwargs.get("m")
            day = kwargs.get("d")
            hour = kwargs.get("hour")
            minute = kwargs.get("minute")
            tz = kwargs.get("tz")

        if not year:
            raise ValueError("Year must be specified")

        if day and not month:
            raise ValueError("If day is specified, month must also be specified")

        if not {hour, minute, tz} <= {None, ""}:
            # Some time element has been specified
            if not day or not month:
                raise ValueError("If any time fields are specified, day and month must also be specified")
            # Now we know that day, month, and year are all specified
            if {hour, minute, tz} & {None, ""}:
                # Although we know that some time element has been specified, not all of them are
                raise ValueError("If any of hour, minute, or timezone is specified, all must be specified")

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

        # Now deal with the time values.
        if hour:
            if not (0 <= int(hour) < 24):
                raise ValueError("Hour must be between 0 and 23.")
        if minute:
            if not (0 <= int(minute) < 60):
                raise ValueError("Minute must be between 0 and 59.")
        if tz is not None:
            if not TZ_PATTERN.match(tz):
                raise ValueError("Timezone must be in the format Area/Location (e.g., America/Chicago).")

        kwargs = {
            "y": f"{year}",
            "m": f"{month:>02}",
            "d": f"{day:>02}",
            "hour": hour,
            "minute": minute,
            "tz": tz
        }
        return super().__call__(*args, **kwargs)

# All dates are stored in the DB as strings formatted as "yyyy.mm.dd" or as
# "yyyy.mm.dd HH:MM tz". Using this format means that comparing and sorting
# dates is as easy as comparing and sorting strings. For fuzzy dates (e.g.,
# just a year or just a year and a month), we use a value of "00" in place
# of the missing month and/or day. Fuzzy dates can then be sorted with non-
# fuzzy dates.
class FuzzyDate(str, metaclass=CustomMeta):
    def __new__(cls, **kwargs):
        base = "{y}.{m}.{d}".format(**kwargs)
        hour = kwargs.get("hour")
        minute = kwargs.get("minute")
        tz = kwargs.get("tz")
        if not {hour, minute, tz} & {None, ""}:
            try:
                hour = f"{int(hour):02}"
                minute = f"{int(minute):02}"
            except (ValueError, TypeError):
                raise ValueError("Hour and minute must be valid integers for formatting.")
            base += f" {hour}:{minute} {tz}"            

        return super().__new__(cls, base)

    def __init__(self, **kwargs):
        self.year = kwargs["y"]
        self.month = kwargs["m"] if kwargs["m"] != "00" else ""
        self.day = kwargs["d"] if kwargs["d"] != "00" else ""
        self.hour = kwargs.get("hour")
        self.minute = kwargs.get("minute")
        self.tz = kwargs.get("tz")
        return super().__init__()

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

    def decompress(self, value):
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
        if value:
            # Values coming from the DB should be in the format yyyy.mm.dd with an optional time and timezone
            return FuzzyDate(value)
        # else
        return value

    def to_python(self, value):
        if value and not isinstance(value, FuzzyDate):
            try:
                if m := DATE_PATTERN.match(value):
                    y, m, d, hour, minute, tz = m.groups()
                    value = FuzzyDate(y=y, m=m, d=d, hour=hour, minute=minute, tz=tz)
                else:
                    raise ValidationError("Dates must be formatted as yyyy, yyyy.mm, yyyy.mm.dd, or full timestamp with time and timezone")
            except TypeError as e:
                raise ValidationError(e)
        return value
