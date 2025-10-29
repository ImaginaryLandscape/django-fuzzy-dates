import calendar
import re
from datetime import date, datetime, timezone
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from zoneinfo import available_timezones, ZoneInfo


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
                if seed.tzinfo is None or seed.tzinfo == timezone.utc:
                    tz_key = "Etc/UTC"
                elif not hasattr(seed.tzinfo, "key"):
                    raise ValueError("Datetime must use a named IANA timezone or 'Etc/UTC'.")
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

            year = norm(kwargs.pop("y", ""))
            month = norm(kwargs.pop("m", ""))
            day = norm(kwargs.pop("d", ""))
            hour = norm(kwargs.pop("hour", ""))
            minute = norm(kwargs.pop("minute", ""))
            tz = norm(kwargs.pop("tz", ""))

            if kwargs:
                raise ValueError(f"Unexpected keyword arguments when creating FuzzyDate: {', '.join(kwargs.keys())}")


        # Now we have all our values stored in variables "year", "month", etc.  Depending on the type
        # of seed, some values may be integers.  We must coerce them all to strings before we're done.
        if not {year, month, day, hour, minute, tz} <= {None, ""}:
            # Some date or time element has been specified
            if not year:
                raise ValueError("Year must be specified if any other date or time component is specified")

            try:
                year = f"{int(year):04}"
            except (ValueError, TypeError):
                raise ValueError("Year must be an integer")
            
            if day and not month:
                raise ValueError("If day is specified, month must also be specified")

            try:
                month = f"{int(month):02}" if month not in ("", None, "00") else "00"
                day = f"{int(day):02}" if day not in ("", None, "00") else "00"
            except (ValueError, TypeError):
                raise ValueError("Month and date, if provided, must be integers")

            # At this point, year, month and day should all be valid numerical strings.
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
            try:
                date(year=int_year, month=int_month, day=int_day)
            except ValueError as e:
                raise e

            # Now we know the date is not invalid, though it may still be fuzzy.
            base = f"{year}.{month}.{day}"

            # Now deal with the time values.
            if not {hour, minute, tz} <= {None, ""}:
                # Some time element has been specified
                if day == "00":
                    raise ValueError("If any time fields are specified, day and month must also be specified")

                # Now we know that day, month, and year are all specified and not fuzzy
                if {hour, minute, tz} & {None, ""}:
                    # Although we know that some time element has been specified, not all of them are
                    raise ValueError("If any of hour, minute, or timezone is specified, all must be specified")

                try:
                    hour = f"{int(hour):02}"
                except (ValueError, TypeError):
                    raise ValueError("Hour must be an integer.")
                if not (0 <= int(hour) < 24):
                    raise ValueError("Hour must be between 0 and 23.")
                try:
                    minute = f"{int(minute):02}"
                except (ValueError, TypeError):
                    raise ValueError("Minute must be an integer.")
                if not (0 <= int(minute) < 60):
                    raise ValueError("Minute must be between 0 and 59.")

                if not TZ_PATTERN.match(tz):
                    raise ValueError("Timezone must be in the format Area/Location (e.g., America/Chicago).")

                base += f" {hour}:{minute} {tz}"

        instance = super().__new__(cls, base)
        instance.year = year
        instance.month = "" if month == "00" else month
        instance.day = "" if day == "00" else day
        instance.hour = hour
        instance.minute = minute
        instance.tz = tz
        return instance

    def __iter__(self):
        # Map component names to instance attributes
        component_map = {
            'y': self.year,
            'm': self.month,
            'd': self.day,
        }

        # Build ordered date components
        components = [component_map[c] for c in DATE_FIELD_ORDER]

        # Optionally add time parts if they are defined
        if self.has_time() and self.has_timezone():
            components.extend([self.hour, self.minute, self.tz])

        return iter(components)
    
    def __repr__(self):
        return f"FuzzyDate({super().__repr__()})"

    def __str__(self):
        data_dict = dict(zip("ymd", self.as_list()))
        date_part = DATE_FIELD_SEPARATOR.join(
            [data_dict[el].lstrip(TRIM_CHAR) for el in DATE_FIELD_ORDER if data_dict[el]]
        )
        if self.has_time() and self.has_timezone():
            return f"{date_part} {self.hour}:{self.minute} {self.tz}"
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

    def has_time(self):
        return self.hour not in ("", None) and self.minute not in ("", None)

    def has_timezone(self):
        return bool(self.tz)

    def has_datetime(self):
        return not self.is_fuzzy and self.has_time() and self.has_timezone()

    @property
    def is_fuzzy(self):
        return self.day == ""

    def to_date(self):
        """
        Convert this FuzzyDate instance to a date object, if possible
        """
        if self.is_fuzzy:
            return None
        # else
        try:
            return date(
                year=int(self.year),
                month=int(self.month),
                day=int(self.day)
            )
        except Exception as e:
            raise ValueError(f"Unable to convert FuzzyDate to date: {e}")

    def to_datetime(self):
        """
        Convert this FuzzyDate instance to a timezone-aware datetime.datetime object, if possible
        """
        if not self.has_datetime():
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
            forms.Select(choices=EMPTY_CHOICE + tuple([(name, name) for name in sorted(available_timezones())]))
        ]
        super().__init__(widgets, attrs)
        for widget_name, widget in zip(self.widgets_names, self.widgets):
            widget.attrs["data-name"] = widget_name

    def decompress(self, value):
        if value:  # will be a FuzzyDate object
            data_dict = dict(zip("ymd", value.as_list()))
            time_str = f"{value.hour}:{value.minute}" if value.has_time() else ""
            retlist = [data_dict[el] for el in DATE_FIELD_ORDER]  # rearrange to the user's preferred order
            retlist += [time_str, value.tz or ""]
            return retlist
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

    def to_python(self, value):
        if isinstance(value, FuzzyDate):
            return value
        if value in self.empty_values:
            return FuzzyDate()
        try:
            return FuzzyDate(value)
        except ValueError as e:
            raise ValidationError(e)


# Custom lookup to handle IS NULL and IS NOT NULL for FuzzyDateField,
@FuzzyDateField.register_lookup
class FuzzyIsNullLookup(models.lookups.IsNull):
    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        if self.rhs:
            return f"({lhs} IS NULL OR {lhs} = '')", lhs_params
        else:
            return f"({lhs} IS NOT NULL AND {lhs} <> '')", lhs_params


# Mixin to exclude empty strings and NULLs from fuzzy date comparisons
class _FuzzyExcludeEmptyBase:
    """
    Mixin that wraps a comparison operator with exclusion of NULL and empty strings.
    """
    operator = None  # must be overridden by subclasses

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        # combine parameters and build guarded SQL
        sql = f"({lhs} <> '' AND {lhs} IS NOT NULL AND {lhs} {self.operator} {rhs})"
        params = lhs_params + rhs_params
        return sql, params


@FuzzyDateField.register_lookup
class FuzzyLessThan(_FuzzyExcludeEmptyBase, models.lookups.LessThan):
    operator = "<"


@FuzzyDateField.register_lookup
class FuzzyLessThanOrEqual(_FuzzyExcludeEmptyBase, models.lookups.LessThanOrEqual):
    operator = "<="


@FuzzyDateField.register_lookup
class FuzzyGreaterThan(_FuzzyExcludeEmptyBase, models.lookups.GreaterThan):
    operator = ">"


@FuzzyDateField.register_lookup
class FuzzyGreaterThanOrEqual(_FuzzyExcludeEmptyBase, models.lookups.GreaterThanOrEqual):
    operator = ">="
