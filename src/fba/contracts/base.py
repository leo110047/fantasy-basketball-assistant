from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Text = Annotated[str, Field(min_length=1)]
FormatVersion = Annotated[int, Field(ge=1, le=1)]
PositiveInt = Annotated[int, Field(gt=0)]
Natural = Annotated[int, Field(ge=0)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Finite = Annotated[float, Field(allow_inf_nan=False)]


class Record(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False)


class ConfigError(ValueError):
    """Invalid configuration; message identifies the field."""


class DataError(ValueError):
    """Invalid or unavailable source; message identifies the artifact."""


class IdentityError(DataError):
    def __init__(self, unresolved: tuple[str, ...]) -> None:
        self.unresolved = unresolved
        super().__init__("identity_map: unresolved players: " + ", ".join(unresolved))


class VersionError(DataError):
    """Storage format is unsupported."""
