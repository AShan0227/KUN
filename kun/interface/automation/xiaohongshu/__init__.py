"""小红书 (xiaohongshu / RedNote) automation (L6.D-内容分发).

小红书特点 (相对微信公众号):
  - 创作者后台 (https://creator.xiaohongshu.com) 是主战场
  - 无创作者写 API (营销 API 限于品牌主 / 蒲公英平台, 个人创作者用不了)
  - 反爬严: 频繁旋转的 anti-bot challenge / sliding captcha
  - 笔记类型: 图文 (≤9 张图) / 视频 / 直播预告
  - 标签 / 话题 / 地点是核心字段, 影响推荐分发

Operations:
  - publish_note: 发笔记 (图文 或 视频, 默认存草稿)
  - list_notes: 列创作者笔记
  - get_note_stats: 查笔记数据 (阅读 / 点赞 / 收藏 / 评论)
"""

from kun.interface.automation.xiaohongshu.browser import XiaohongshuBrowserAdapter

__all__ = ["XiaohongshuBrowserAdapter"]
