"""json_validate starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import json_validate
from kun.skills.dispatcher import register

register("json_validate", json_validate)
