"""code_lint starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import code_lint
from kun.skills.dispatcher import register

register("code_lint", code_lint)
