from pydantic import BaseModel, Field, ConfigDict
from typing import Optional

class AccessPoint(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    
    bssid: str
    ssid: Optional[str] = Field(default=None)
    channel: int = Field(default=1)
    signal: int = Field(default=-100)
    encryption: Optional[str] = Field(default="Unknown")
    beacons: int = Field(default=0)

# TODO: class Client (name?) representing an AP client (bottom table in airodump-ng).
