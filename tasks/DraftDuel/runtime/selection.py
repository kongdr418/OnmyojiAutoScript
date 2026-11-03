"""选人轮次、队友推荐及策略选择。"""

import re
import time

from module.base.timer import Timer
from module.logger import logger
from tasks.DraftDuel.strategy import CATALOG, rank_picks, normalize_name
from tasks.DraftDuel.matrix import joint_draft_choices


class DraftDuelSelectionMixin:
    def _draft_phase(self) -> bool:
        """选满五位式神；是否等待队友推荐由配置决定。"""
        logger.hr('Draft selection', 2)
        use_recommendation = self.config.draft_duel.draft_duel_config.use_teammate_recommendation
        logger.info(f'DraftDuel: teammate recommendation={use_recommendation}')
        picked = set()
        self.picked_roles = []
        self._team = []
        self._draft_started_at = time.time()
        self._draft_exchange_waited = set()
        self._draft_share_write([])
        last_hint = None
        timer = Timer(300).start()
        while not timer.reached():
            self.screenshot()
            if self.appear(self.I_DRAFT_DUEL_COMPLETED_HALL):
                logger.info('DraftDuel: selection finished')
                logger.info(f'DraftDuel: lineup {self.picked_roles}')
                if not self._bind_lobby_faces():
                    self.device.sleep(0.3)
                    continue
                return True
            if not self.appear(self.I_SELECTION_PAGE):
                self.device.sleep(0.5)
                continue

            role_text = self._ocr_text(self.O_HINT_ROLE)
            time_text = self._ocr_text(self.O_HINT_TIME)
            index = self._parse_pick_index(role_text)
            seconds = self._parse_seconds(time_text)
            hint = (index, seconds)
            if hint != last_hint:
                logger.info(
                    f'DraftDuel: pick={index}, hint=[{role_text}] [{time_text}], {seconds}s'
                )
                last_hint = hint

            if not 1 <= index <= self.TOTAL_PICKS or index in picked:
                self.device.sleep(0.5)
                continue
            self._draft_round = index
            # 推荐等待期间也先公开候选，供另一端策略使用。
            self._draft_offers = [normalize_name(self._role_name(slot)) for slot in range(3)]
            self._draft_share_write(self._team, self._draft_offers, index)
            if index not in self._draft_exchange_waited and (seconds <= 0 or seconds > 3):
                self._draft_exchange_waited.add(index)
                # 只给同机消息一个短暂交换窗口，不等待人工推荐或长期阻塞选卡。
                for _ in range(5):
                    snapshot = self._draft_share_read_snapshot()
                    if snapshot.get('round') == index and snapshot.get('offers'):
                        break
                    self.device.sleep(0.2)
            # 直接策略模式不依赖倒计时 OCR，候选识别成功即可选择。
            if use_recommendation and not 0 < seconds <= self.RECOMMEND_HIGH:
                self.device.sleep(0.5)
                continue

            slot = self._choose_draft_slot(seconds)
            if slot is None:
                if seconds > self.RECOMMEND_LOW or seconds <= 0:
                    # 等推荐，或候选/倒计时尚未识别完整，下一帧继续尝试。
                    self.device.sleep(0.5)
                    continue
                # 到策略阶段仍无法选卡：不退出，等系统倒计时结束自动选，
                # 否则任务提前结束，调度器会把队伍带走
                logger.warning(f'DraftDuel: pick {index} wait auto select')
                if self._wait_pick_change(index, timeout=seconds + 2):
                    self.picked_roles.append('')
                    picked.add(index)
                    logger.info(f'DraftDuel: pick {index} auto selected')
                    continue
                logger.warning('DraftDuel: pick did not advance after countdown')
                return False

            raw_name = self._role_name(slot)
            name = normalize_name(raw_name) or raw_name
            if name != raw_name:
                logger.info(f'DraftDuel: normalize name {raw_name!r} -> {name!r}')
            self.click(self._choose_slots()[slot], interval=1)
            if not self._wait_pick_change(index):
                logger.warning('DraftDuel: pick did not advance, retry')
                self.device.sleep(1)
                continue
            self.picked_roles.append(name)
            self._team.append(name)
            self._draft_share_write(self._team)
            logger.info(f'DraftDuel: pick {index} slot {slot + 1} -> {name}')
            picked.add(index)
            self.device.sleep(1)
        logger.warning('DraftDuel: selection timeout')
        return False

    def _wait_pick_change(self, index: int, timeout: float = 3) -> bool:
        timer = Timer(timeout).start()
        while not timer.reached():
            self.screenshot()
            text = self._ocr_text(self.O_HINT_ROLE)
            if self._parse_pick_index(text) != index:
                return True
            self.device.sleep(0.5)
        return False

    def _choose_slots(self):
        return (self.C_CHOOSE_1, self.C_CHOOSE_2, self.C_CHOOSE_3)

    def _role_name(self, slot: int) -> str:
        """读三张卡里对应位置的名字。"""
        rules = (self.O_ROLE_1, self.O_ROLE_2, self.O_ROLE_3)
        name = self._ocr_text(rules[slot])
        if not name:
            logger.warning(f'DraftDuel: role {slot + 1} name OCR empty')
        return name

    def _choose_draft_slot(self, seconds: int) -> int | None:
        """关闭推荐时立即策略选卡；开启时保留 30~22 秒的推荐等待窗口。"""
        if self.config.draft_duel.draft_duel_config.use_teammate_recommendation:
            slot = self._recommended_slot()
            if slot is not None or seconds > self.RECOMMEND_LOW:
                return slot
        return self._choose_by_strategy()

    def _recommended_slot(self):
        matches = self.I_RECOMMEND_ROLE.match_all_any(
            self.device.image, frame_id=self.device.image_frame_id
        )
        if not matches:
            return None
        # 匹配结果是 (score, x, y, w, h)，按 x 中心归到左中右三张卡
        slot_centers = (273, 648, 1023)
        for _, x, y, w, h in matches:
            center = x + w / 2
            slot = min(range(3), key=lambda i: abs(center - slot_centers[i]))
            if abs(center - slot_centers[slot]) <= 150:
                return slot
        return None

    def _choose_by_strategy(self):
        """按矩阵比较三张候选，返回卡槽下标 0~2。"""
        raw = [self._role_name(slot) for slot in range(3)]
        names = [normalize_name(name) for name in raw]
        offers = [name for name in names if name]
        if not offers:
            logger.warning(f'DraftDuel: strategy no offers, raw={raw}')
            return None
        try:
            # OCR 可能把未归一化的名字写进队伍，过滤掉图鉴里没有的，避免 KeyError
            team = [item for item in self._team if item in CATALOG]
            snapshot = self._draft_share_read_snapshot()
            mate = snapshot.get('names', [])
            ranked = rank_picks(offers, team, mate=mate, team_size=self.TOTAL_PICKS)
            decision = ranked[0]
            peer_offers = snapshot.get('offers', [])
            if (snapshot.get('round') == getattr(self, '_draft_round', None)
                    and peer_offers and len(snapshot.get('names', [])) == len(team)
                    and len(offers) == 3):
                try:
                    own, peer = joint_draft_choices(offers, team, peer_offers, mate)
                    decision = next(row for row in ranked if row.name == own)
                    logger.info(f'DraftDuel: joint draft own={own} mate_expected={peer} '
                                f'mate_offers={peer_offers}; peer choice is not confirmed')
                except ValueError as e:
                    logger.warning(f'DraftDuel: joint draft fallback to known roster: {e}')
            logger.info(f'DraftDuel: matrix context own={team} mate={mate or "unknown"}')
            for candidate in ranked:
                parts = ' '.join(f'{label}{value:+.1f}' for label, value in candidate.score_parts)
                logger.info(
                    f'DraftDuel: matrix candidate {candidate.name} total={candidate.score:.1f} '
                    f'{parts} lineup={candidate.lineup} '
                    f'reasons={candidate.reasons}'
                )
        except Exception as e:
            logger.warning(f'DraftDuel: strategy failed: {e}')
            return None
        try:
            slot = names.index(decision.name)
        except ValueError:
            logger.warning(f'DraftDuel: strategy pick {decision.name} not in {names}')
            return None
        logger.info(
            f'DraftDuel: strategy {decision.name} score={decision.score:.1f} '
            f'reasons={decision.reasons} souls={decision.suggested_souls}'
        )
        return slot

    @staticmethod
    def _parse_pick_index(text: str) -> int:
        match = re.search(r'第\s*(\d+)', text or '')
        if match:
            return int(match.group(1))
        nums = re.findall(r'\d+', text or '')
        return int(nums[0]) if nums else 0

    @staticmethod
    def _parse_seconds(text: str) -> int:
        # 倒计时最多 30 秒
        match = re.search(r'(\d{1,2})\s*[sS]', text or '')
        if match:
            return min(30, int(match.group(1)))
        digits = re.sub(r'\D', '', text or '')
        if not digits:
            return 0
        return min(30, int(digits[:2]))
