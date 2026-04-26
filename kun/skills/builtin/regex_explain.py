"""regex_explain starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import regex_explain
from kun.skills.dispatcher import register

register("regex_explain", regex_explain)
