"""time_zone_convert starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import time_zone_convert
from kun.skills.dispatcher import register

register("time_zone_convert", time_zone_convert)
