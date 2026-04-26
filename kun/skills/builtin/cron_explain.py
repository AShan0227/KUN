"""cron_explain starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import cron_explain
from kun.skills.dispatcher import register

register("cron_explain", cron_explain)
