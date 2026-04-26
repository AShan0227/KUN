"""image_describe starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import image_describe
from kun.skills.dispatcher import register

register("image_describe", image_describe)
