"""协同对弈界面坐标、识别规则与运行常量。"""

from module.atom.frame import Click, Image, RuleFrame


class DraftDuelSettingsMixin:
    AVAILABLE_AMOUNTS = ('30万', '300000')
    FULL_AMOUNTS = ('100000', '10万')
    # 选卡阶段每个账号选 5 位；上阵阶段再各上 3 位（合计 6）
    TOTAL_PICKS = 5
    FIELD_ROUNDS = 3
    # 沿用 self 项目的手牌搜索区域、内部头像裁剪和锚点模板（1280x720）。
    HAND_AREA = (8, 486, 1021, 223)
    F_SHIKIGAMI_HEAD = RuleFrame(
        name='shikigami_head',
        roi_badge=(422, 14, 32, 84),
        roi_range=[(478, 45, 35, 31), (563, 46, 35, 31), (646, 44, 35, 31), (733, 46, 35, 31), (820, 44, 35, 31)],
        roi_back=(410, 5, 512, 103),
        threshold=0.8,
        method='Template matching',
        profile='High',
        badgefile=['./tasks/DraftDuel/draft_duel/shikigami_head_badge_1.png'],
        range=[Image() for _ in range(5)],
    )
    F_FIGHT_SHIKIGAMI = RuleFrame(
        name='fight_shikigami',
        roi_badge=(559, 32, 33, 60),
        roi_range=[(86, 34, 67, 66), (158, 34, 67, 66), (229, 34, 67, 66), (302, 34, 67, 66), (374, 35, 67, 66), (445, 34, 67, 66)],
        roi_back=(73, 14, 536, 94),
        threshold=0.8,
        method='Template matching',
        profile='High',
        badgefile=['./tasks/DraftDuel/draft_duel/fight_shikigami_badge_1.png'],
        range=[Image() for _ in range(6)],
    )
    F_FIGHT_SHIKIGAMI_SET = RuleFrame(
        name='fight_shikigami_set',
        roi_badge=(28, 369, 54, 44),
        roi_range=[(183, 330, 67, 66), (340, 333, 67, 66), (514, 360, 67, 66),
                   (690, 369, 67, 66), (858, 344, 67, 66), (1022, 319, 67, 66)],
        roi_back=(0, 220, 1147, 276),
        threshold=0.8,
        method='Template matching',
        badgefile=['./tasks/DraftDuel/draft_duel/fight_shikigami_set_badge_1.png'],
        range=[Click() for _ in range(6)],
    )
    AUTO_FORM_AFTER_FIRST = True
    # 队友推荐最多等到倒计时 22s（30-8）；22s 还没推荐就走策略
    RECOMMEND_HIGH = 30
    RECOMMEND_LOW = 22
    # 懒加载的 v6-small 识别模型；False 表示加载失败，回退共享 OCR
    _v6_ocr_model = None
