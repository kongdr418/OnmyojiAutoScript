"""大厅头像绑定、手牌及上阵头像识别。"""

from module.logger import logger
from tasks.DraftDuel.strategy import normalize_name
from tasks.DraftDuel.visual_cards import CARD_BOXES, match_hand
from tasks.DraftDuel.runtime.portrait_match import FIGHT_PORTRAIT_SCALE, portrait_score


class DraftDuelRecognitionMixin:
    def _bind_lobby_faces(self):
        """选完后把大厅顶部头像按顺序和 picked_roles 绑定。"""
        faces = self.runframe(self.F_SHIKIGAMI_HEAD)
        if (not faces or len(faces) != 5 or len(faces) != len(self.picked_roles)
                or any(face.size == 0 for face in faces)):
            logger.warning(
                f'DraftDuel: lobby faces unavailable or incomplete; picks={len(self.picked_roles)}'
            )
            return False
        self._lobby_faces = [(name, face.copy()) for name, face in zip(self.picked_roles, faces)]
        logger.info(f'DraftDuel: lobby faces bound to {self.picked_roles}')
        return True

    def _match_hand_cards(self):
        """上阵界面手牌卡位 -> 式神名；只截图，不点卡。"""
        pairs = getattr(self, '_lobby_faces', None)
        if not pairs:
            logger.warning('DraftDuel: no lobby faces bound, skip hand matching')
            return {}
        result = {}
        for slot, index, score in match_hand(self.device.image, [f for _, f in pairs]):
            name = pairs[index][0]
            result[slot] = (name, score)
            tag = ' (elimination)' if score < 0.6 else ''
            logger.info(f'DraftDuel: hand card {slot + 1} -> {name}, score={score:.3f}{tag}')
        return result

    def _card_marked(self, slot: int) -> bool:
        """按 dd.json 的 roiBack 搜标记，再按目标卡位的 x 过滤。"""
        matches = self.I_MARK.match_all(
            self.device.image, threshold=self.I_MARK.threshold,
            frame_id=self.device.image_frame_id,
        )
        if not matches:
            return False
        x, _y, w, _h = CARD_BOXES[slot]
        center_x = x + w / 2
        return any(abs(match[1] - center_x) <= w / 2 + 15 for match in matches)

    def _field_slot_actual(self, index: int):
        """读己方第 index 个上阵位现在显示是谁。"""
        portraits = self.runframe(self.F_FIGHT_SHIKIGAMI)
        if not portraits or len(portraits) != 6:
            return None, 0.0
        offset = 0 if self._configured_role() == 'captain' else 3
        region = portraits[offset + index]
        ranked = []
        for name, face in getattr(self, '_lobby_faces', None) or []:
            score = portrait_score(face, region, scale=FIGHT_PORTRAIT_SCALE)
            ranked.append((score, name))
        ranked.sort(reverse=True)
        if not ranked:
            return None, 0.0
        best_score, best_name = ranked[0]
        # 与 self 项目一致：最低分 0.8，且领先第二名至少 0.05。
        if best_score < 0.8 or (len(ranked) > 1 and best_score - ranked[1][0] < 0.05):
            return None, best_score
        return best_name, best_score

    def _screen_text(self) -> str:
        try:
            results = self.O_SCREEN.detect_and_ocr(self.device.image)
        except Exception as e:
            logger.warning(f'DraftDuel: screen OCR failed: {e}')
            return ''
        return ''.join(str(item.ocr_text or '') for item in results)

    def _read_lineup_detail(self):
        """选后大厅打开「阵容详情」，OCR 5 个名字并和 picked_roles 对照。"""
        self.screenshot()
        opened = False
        for card in (self.C_DETAIL_CARD_1, self.C_DETAIL_CARD_2, self.C_DETAIL_CARD_3,
                     self.C_DETAIL_CARD_4, self.C_DETAIL_CARD_5):
            self.click(card, interval=0.4)
            self.device.sleep(0.4)
            self.screenshot()
            title = str(self.O_DETAIL_TITLE.ocr(self.device.image) or '')
            if '阵容详情' in title or '阵容详情' in self._screen_text():
                opened = True
                break
        if not opened:
            logger.warning('DraftDuel: lineup detail not opened')
            return None

        names = []
        try:
            self.click(self.C_MINE_TAB, interval=0.3)
            self.device.sleep(0.4)
            for _ in range(3):
                self.screenshot()
                names = []
                for rule in (self.O_LINEUP_NAME_1, self.O_LINEUP_NAME_2,
                             self.O_LINEUP_NAME_3, self.O_LINEUP_NAME_4,
                             self.O_LINEUP_NAME_5):
                    raw = str(rule.ocr(self.device.image) or '')
                    names.append(normalize_name(raw) or raw)
                if all(names):
                    break
                self.device.sleep(0.3)
            logger.info(f'DraftDuel: lineup detail names {names}')
            if all(names) and set(names) != set(self.picked_roles):
                logger.warning(
                    f'DraftDuel: lineup detail differs from picks: {names} vs {self.picked_roles}'
                )
            self._battle_detail = tuple(names) if all(names) else None
            return self._battle_detail
        finally:
            self.click(self.C_DETAIL_CLOSE, interval=0.2)
