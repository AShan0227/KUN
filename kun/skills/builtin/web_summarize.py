"""web_summarize starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import web_summarize
from kun.skills.dispatcher import register

register("web_summarize", web_summarize)
