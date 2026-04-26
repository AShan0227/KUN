"""markdown_to_docx starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import markdown_to_docx
from kun.skills.dispatcher import register

register("markdown_to_docx", markdown_to_docx)
