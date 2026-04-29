"""git_diff_review starter-pack skill."""

from kun.skills.builtin.starter_pack_utils import git_diff_review
from kun.skills.dispatcher import register

register("git_diff_review", git_diff_review)
