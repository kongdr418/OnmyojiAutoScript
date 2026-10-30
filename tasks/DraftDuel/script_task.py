# This Python file uses the following encoding: utf-8
import re

from module.exception import TaskEnd
from module.logger import logger

from tasks.DraftDuel.assets import DraftDuelAssets
from tasks.DraftDuel.config import DraftDuelConfig
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_draft_duel


class ScriptTask(GameUi, DraftDuelAssets):
    """协同对弈（Draft Duel）。"""

    AVAILABLE_TEXTS = ('本次免费',)
    AVAILABLE_AMOUNTS = ('30万', '300000')
    FULL_AMOUNTS = ('100000', '10万')

    def run(self):
        con: DraftDuelConfig = self.config.draft_duel.draft_duel_config
        logger.hr('Draft Duel', 2)

        self.goto_page(page_draft_duel)
        logger.info('DraftDuel: lobby reached')
        logger.info(f'DraftDuel: limit_time={con.limit_time}')

        state = self._cost_state()
        if state == 'full':
            logger.info('DraftDuel: weekly attempts are full')
            self.set_next_run(task='DraftDuel', success=True, finish=True)
            raise TaskEnd('DraftDuel')
        if state == 'unknown':
            logger.warning('DraftDuel: unrecognized cost text, stop without entering a match')
            self.set_next_run(task='DraftDuel', success=False, finish=True)
            raise TaskEnd('DraftDuel')

        logger.info('DraftDuel: attempts available, flow after lobby is not implemented yet')
        self.set_next_run(task='DraftDuel', success=True, finish=True)
        raise TaskEnd('DraftDuel')

    def _cost_state(self) -> str:
        text = self.O_GOLD.ocr(self.device.image)
        state = self._parse_cost_text(text)
        logger.info(f'DraftDuel: cost OCR [{text}] -> {state}')
        return state

    @classmethod
    def _parse_cost_text(cls, text: str) -> str:
        text = (text or '').replace(' ', '').replace('\u3000', '')
        if text in cls.AVAILABLE_TEXTS:
            return 'available'
        # 去掉标点，只留数字和万/亿
        amount = re.sub(r'[^\d万亿]', '', text)
        if amount in cls.AVAILABLE_AMOUNTS:
            return 'available'
        if amount in cls.FULL_AMOUNTS:
            return 'full'
        return 'unknown'
