"""sql_query starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import sql_query
from kun.skills.dispatcher import register

register("sql_query", sql_query)
