"""协同对弈界面坐标、识别规则与运行常量。"""

class DraftDuelSettingsMixin:
    AVAILABLE_AMOUNTS = ('30万', '300000')
    FULL_AMOUNTS = ('100000', '10万')
    # 选卡阶段每个账号选 5 位；上阵阶段再各上 3 位（合计 6）
    TOTAL_PICKS = 5
    FIELD_ROUNDS = 3
    # 沿用 self 项目的手牌搜索区域、内部头像裁剪和锚点模板（1280x720）。
    HAND_AREA = (8, 486, 1021, 223)
    AUTO_FORM_AFTER_FIRST = True
    # 队友推荐最多等到倒计时 22s（30-8）；22s 还没推荐就走策略
    RECOMMEND_HIGH = 30
    RECOMMEND_LOW = 22
    # 懒加载的 v6-small 识别模型；False 表示加载失败，回退共享 OCR
    _v6_ocr_model = None
