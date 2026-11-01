"""协同对弈任务入口；各阶段实现在 runtime 包。"""

from module.logger import logger
from tasks.Component.GeneralInvite.general_invite import GeneralInvite
from tasks.DraftDuel.assets import DraftDuelAssets
from tasks.DraftDuel.config import DraftDuelConfig, UserStatus
from tasks.GameUi.game_ui import GameUi
from tasks.DraftDuel.runtime.lifecycle import DraftDuelLifecycleMixin
from tasks.DraftDuel.runtime.invitation import DraftDuelInvitationMixin
from tasks.DraftDuel.runtime.ocr import DraftDuelOcrMixin
from tasks.DraftDuel.runtime.selection import DraftDuelSelectionMixin
from tasks.DraftDuel.runtime.recognition import DraftDuelRecognitionMixin
from tasks.DraftDuel.runtime.deployment import DraftDuelDeploymentMixin
from tasks.DraftDuel.runtime.cooperation import DraftDuelCooperationMixin
from tasks.DraftDuel.runtime.battle import DraftDuelBattleMixin
from tasks.DraftDuel.runtime.settings import DraftDuelSettingsMixin


class ScriptTask(
    DraftDuelLifecycleMixin,
    DraftDuelInvitationMixin,
    DraftDuelOcrMixin,
    DraftDuelSelectionMixin,
    DraftDuelRecognitionMixin,
    DraftDuelDeploymentMixin,
    DraftDuelCooperationMixin,
    DraftDuelBattleMixin,
    DraftDuelSettingsMixin,
    GameUi,
    GeneralInvite,
    DraftDuelAssets,
):
    """按配置执行队长或队员流程。"""

    def run(self):
        con: DraftDuelConfig = self.config.draft_duel.draft_duel_config
        logger.hr('Draft Duel', 2)
        self._require_open_for_match()

        if con.user_status == UserStatus.MEMBER:
            self._run_member(con)
        else:
            self._run_leader(con)
