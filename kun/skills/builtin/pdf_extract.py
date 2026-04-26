"""pdf_extract starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import pdf_extract
from kun.skills.dispatcher import register

register("pdf_extract", pdf_extract)
