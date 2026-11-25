from tasks.GameUi.assets import GameUiAssets
from tasks.GameUi.default_pages import page_collection, page_guild, page_main
from tasks.GameUi.matcher import all_of, not_
from tasks.GameUi.page_definition import Page
from tasks.GlobalGame.assets import GlobalGameAssets
from tasks.WeeklyTrifles.assets import WeeklyTriflesAssets

page_shikigami_collection = Page(WeeklyTriflesAssets.I_WT_COLLECTION_TITLE)
page_shikigami_collection.add_enter_success_hooks(WeeklyTriflesAssets.I_WT_COLLECTION_GUIDE)
page_shikigami_collection.add_enter_failure_hooks(GlobalGameAssets.I_UI_CANCEL_SAMLL)
page_collection.connect(
    page_shikigami_collection,
    GameUiAssets.I_CHECK_COLLECTION,
    key="page_collection->page_shikigami_collection",
)
page_shikigami_collection.connect(
    page_collection,
    GlobalGameAssets.I_UI_BACK_YELLOW,
    key="page_shikigami_collection->page_collection",
)

page_shikigami_scroll = Page(all_of(
    WeeklyTriflesAssets.I_WT_COLLECT,
    not_(WeeklyTriflesAssets.I_WT_COLLECT_WECHAT),
    not_(WeeklyTriflesAssets.I_WT_COLLECT_QR_CODE),
))
page_shikigami_collection.connect(
    page_shikigami_scroll,
    WeeklyTriflesAssets.I_WT_COLLECTION_SCROLL,
    key="page_shikigami_collection->page_shikigami_scroll",
)
page_shikigami_scroll.connect(
    page_shikigami_collection,
    GlobalGameAssets.I_UI_BACK_BLUE,
    key="page_shikigami_scroll->page_shikigami_collection",
)

page_shikigami_share = Page(WeeklyTriflesAssets.I_WT_COLLECT_WECHAT)
page_shikigami_scroll.connect(
    page_shikigami_share,
    WeeklyTriflesAssets.I_WT_COLLECT,
    key="page_shikigami_scroll->page_shikigami_share",
)
page_shikigami_share.connect(
    page_shikigami_scroll,
    WeeklyTriflesAssets.I_WT_COLLECT,
    key="page_shikigami_share->page_shikigami_scroll",
)

page_shikigami_share_qr = Page(all_of(
    WeeklyTriflesAssets.I_WT_COLLECT,
    WeeklyTriflesAssets.I_WT_COLLECT_QR_CODE,
))
page_shikigami_share_qr.connect(
    page_shikigami_scroll,
    WeeklyTriflesAssets.C_WT_WECHAT,
    key="page_shikigami_share_qr->page_shikigami_scroll",
)

# 摸鱼行动页面
page_touch_fish = Page(WeeklyTriflesAssets.I_CHECK_TOUCH_FISH)
page_guild.connect(
    page_touch_fish,
    WeeklyTriflesAssets.I_GUILD_GOTO_TF,
    key="page_guild->page_touch_fish",
)
page_touch_fish.connect(
    page_main,
    WeeklyTriflesAssets.I_WT_TF_GOTO_MAIN,
    key="page_touch_fish->page_main",
)
