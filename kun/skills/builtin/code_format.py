"""code_format starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import code_format
from kun.skills.dispatcher import register

register("code_format", code_format)
