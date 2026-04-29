"""csv_analyze starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import csv_analyze
from kun.skills.dispatcher import register

register("csv_analyze", csv_analyze)
