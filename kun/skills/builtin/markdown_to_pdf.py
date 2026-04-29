"""markdown_to_pdf starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import markdown_to_pdf
from kun.skills.dispatcher import register

register("markdown_to_pdf", markdown_to_pdf)
