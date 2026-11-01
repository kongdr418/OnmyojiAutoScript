"""协同对弈专用 OCR 与金币消耗识别。"""

import re

from module.logger import logger


class DraftDuelOcrMixin:
    def _load_v6_ocr(self):
        if self._v6_ocr_model is not None:
            return self._v6_ocr_model
        try:
            from tasks.DraftDuel.v6_rec import V6Rec
            self._v6_ocr_model = V6Rec()
            logger.info('DraftDuel: v6-small rec loaded')
        except Exception as e:
            logger.warning(f'DraftDuel: v6 OCR unavailable, fallback to shared OCR: {e}')
            self._v6_ocr_model = False
        return self._v6_ocr_model

    def _ocr_text(self, rule) -> str:
        """协同用 v6-small 识别，加载失败就回退到共享的 v2 服务。"""
        model = self._load_v6_ocr()
        if model:
            x, y, w, h = rule.roi
            crop = self.device.image[y:y + h, x:x + w]
            try:
                text, score = model.recognize_single(crop)
                if score < 0.6:
                    logger.warning(
                        f'DraftDuel: v6 OCR {rule.name} low score {score:.3f}: [{text}]'
                    )
                    return ''
                return text.strip()
            except Exception as e:
                logger.warning(f'DraftDuel: v6 OCR {rule.name} failed: {e}')
        return rule.ocr(self.device.image).strip()

    def _cost_state(self) -> str:
        text = self._ocr_text(self.O_GOLD)
        state = self._parse_cost_text(text)
        logger.info(f'DraftDuel: cost OCR [{text}] -> {state}')
        return state

    @classmethod
    def _parse_cost_text(cls, text: str) -> str:
        text = (text or '').replace(' ', '').replace('\u3000', '')
        if '免费' in text:
            return 'available'
        # 去掉标点，只留数字和万/亿
        amount = re.sub(r'[^\d万亿]', '', text)
        if amount in cls.AVAILABLE_AMOUNTS:
            return 'available'
        if amount in cls.FULL_AMOUNTS:
            return 'full'
        return 'unknown'
