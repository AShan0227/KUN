"""translate starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import translate
from kun.skills.dispatcher import register

register("translate", translate)
