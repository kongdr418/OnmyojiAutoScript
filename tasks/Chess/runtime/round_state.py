"""Chess runtime: round OCR, board occupancy, and state readers."""

# This Python file uses the following encoding: utf-8

import random
import re
import time
from functools import cached_property
from pathlib import Path

import cv2
import numpy as np
from module.logger import logger
from tasks.Chess.strategy.grigri import (
    grigri_category,
    grigri_file,
    grigri_keep_names,
    grigri_names_for_quality,
    grigri_score,
    grigri_selection_key,
    resolve_grigri_name,
)


class ChessRoundStateMixin:
    """Internal mixin; use through ``ScriptTask`` only."""

    @cached_property
    def grigri_icon_templates(self) -> dict[str, object]:
        """按中文名缓存全部符咒图标，供 OCR 失败时进行图像兜底。"""
        badge_dir = Path(__file__).resolve().parents[1] / 'resource' / 'badge'
        templates = {}
        for name in grigri_names_for_quality(None):
            file = badge_dir / grigri_file(name)
            image = cv2.imread(str(file), cv2.IMREAD_COLOR)
            if image is None:
                logger.warning(f'Chess grigri icon is missing: {file}')
                continue
            templates[name] = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        logger.debug(f'Loaded {len(templates)} Chess grigri icon templates')
        return templates

    def _detect_grigri_quality(self) -> str | None:
        for quality, rule in (
            ('gold', self.I_GRIGRI_GOLD),
            ('silver', self.I_GRIGRI_SILVER),
            ('copper', self.I_GRIGRI_COPPER),
        ):
            if self.appear(rule):
                self._grigri_quality_cache = quality
                return quality
        cached_quality = getattr(self, '_grigri_quality_cache', None)
        if cached_quality is not None:
            logger.warning(
                'Chess grigri quality was not recognized after refresh; '
                f'reuse current selection quality={cached_quality}'
            )
            return cached_quality
        logger.warning('Chess grigri quality was not recognized')
        return None

    def _match_grigri_icon(
        self,
        option_rule,
        quality: str | None,
    ) -> tuple[str | None, float]:
        x, y, width, height = option_rule.roi_front
        image_height, image_width = self.device.image.shape[:2]
        # 点击框略窄于 94px 图标，匹配区向左右各扩 24px。
        left = max(0, x - 24)
        top = max(0, y - 8)
        right = min(image_width, x + width + 24)
        bottom = min(image_height, y + height)
        source = self.device.image[top:bottom, left:right]
        best_name, best_score = None, 0.0
        for name in grigri_names_for_quality(quality):
            template = self.grigri_icon_templates.get(name)
            if (
                template is None
                or template.shape[0] > source.shape[0]
                or template.shape[1] > source.shape[1]
            ):
                continue
            result = cv2.matchTemplate(
                source,
                template,
                cv2.TM_CCOEFF_NORMED,
            )
            _, score, _, _ = cv2.minMaxLoc(result)
            if score > best_score:
                best_name, best_score = name, float(score)
        if best_score < self.GRIGRI_ICON_THRESHOLD:
            return None, best_score
        return best_name, best_score

    def _recognize_grigri_options(self) -> list[dict]:
        quality = self._detect_grigri_quality()
        lineup_strategy = self.get_lineup_strategy()
        option_rules = (
            self.C_GRIGRI_OPTION_1,
            self.C_GRIGRI_OPTION_2,
            self.C_GRIGRI_OPTION_3,
        )
        ocr_rules = (
            self.O_GRIGRI_OPTION_NAME_1,
            self.O_GRIGRI_OPTION_NAME_2,
            self.O_GRIGRI_OPTION_NAME_3,
        )
        result = []
        for index, (option_rule, ocr_rule) in enumerate(
            zip(option_rules, ocr_rules),
            start=1,
        ):
            raw = self._normalize_ocr_text(ocr_rule.ocr(self.device.image))
            ocr_name, text_score = resolve_grigri_name(raw, quality)
            icon_name, icon_score = self._match_grigri_icon(
                option_rule,
                quality,
            )
            # OCR 已达到图鉴映射阈值时优先采用文字；图像负责校验和兜底。
            name = ocr_name or icon_name
            if ocr_name and icon_name and ocr_name != icon_name:
                logger.warning(
                    'Chess grigri OCR/icon conflict: '
                    f'option={index}, ocr={ocr_name}({text_score:.3f}), '
                    f'icon={icon_name}({icon_score:.3f})'
                )
            category = grigri_category(name)
            score = grigri_score(name, lineup_strategy)
            selection_key = grigri_selection_key(name, lineup_strategy)
            item = {
                'index': index,
                'quality': quality,
                'raw': raw,
                'name': name,
                'category': category,
                'score': score,
                'selection_key': selection_key,
                'text_score': text_score,
                'icon_name': icon_name,
                'icon_score': icon_score,
                'rule': option_rule,
            }
            result.append(item)
            logger.info(
                'Chess grigri option: '
                f'option={index}, quality={quality or "unknown"}, '
                f'ocr=[{raw}], name={name or "未知"}, '
                f'category={category}, benefit={score:g}, '
                f'icon={icon_name or "未命中"}({icon_score:.3f})'
            )
        return result

    @staticmethod
    def _best_grigri_option(options: list[dict]) -> dict:
        best_key = max(item['selection_key'] for item in options)
        return random.choice([
            item for item in options if item['selection_key'] == best_key
        ])

    def _refresh_grigri_option(self, option: dict) -> bool:
        index = option['index']
        remaining = self._grigri_refresh_remaining
        if remaining[index - 1] <= 0:
            logger.debug(
                f'Chess grigri option {index} has no refresh remaining'
            )
            return False
        logger.info(
            f'Refresh low-score Chess grigri: '
            f'option={index}, benefit={option["score"]:g}, '
            f'remaining={remaining}'
        )
        refresh_rule = getattr(self, f'C_GRIGRI_REFRESH_{index}')
        for click_index in range(1, 3):
            click_x, click_y = refresh_rule.coord()
            self.device.click(
                x=click_x,
                y=click_y,
                control_name=(
                    f'{refresh_rule.name}_{click_index}'
                ),
            )
            logger.debug(
                f'Chess grigri refresh click: '
                f'option={index}, click={click_index}/2'
            )
            time.sleep(self.FAST_OPERATION_INTERVAL)
        remaining[index - 1] -= 1
        logger.info(
            f'Chess grigri refresh recorded: '
            f'option={index}, remaining={remaining}'
        )
        # 两次刷新点击后等待卡面、品质标志和文字动画稳定，再重新识别。
        time.sleep(self.ACTION_SETTLE_INTERVAL)
        self.screenshot()
        return True

    def _continue_grigri_refresh_below_nine(self) -> bool:
        """读取是否启用“刷新收益排名非前40%选项”。"""
        chess_task_config = getattr(self.config, 'chess', None)
        chess_config = getattr(chess_task_config, 'chess_config', None)
        return bool(getattr(
            chess_config,
            'continue_grigri_refresh_below_nine',
            True,
        ))

    def _next_refreshable_grigri_option(
        self,
        options: list[dict],
    ) -> dict | None:
        """选择下一项要刷新的位置；优先刷新当前评分最低项。"""
        candidates = [
            item
            for item in options
            if self._grigri_refresh_remaining[item['index'] - 1] > 0
        ]
        if not candidates:
            return None
        if not self._continue_grigri_refresh_below_nine():
            return None
        if any(item['quality'] is None for item in candidates):
            logger.warning(
                'Skip Chess grigri refresh: quality is unknown, '
                'top-40-percent ranking cannot be calculated safely'
            )
            return None

        lineup_strategy = self.get_lineup_strategy()
        keep_names = {
            quality: grigri_keep_names(
                quality,
                lineup_strategy,
            )
            for quality in {item['quality'] for item in candidates}
        }
        logger.debug(
            'Chess grigri top-40-percent keep counts: '
            f'{ {quality: len(names) for quality, names in keep_names.items()} }'
        )
        # 以当前品质的图鉴总数为基数，严格按数量保留前40%；
        # 同收益只在最终选取时随机，排行榜边界使用固定图鉴顺序。
        candidates = [
            item
            for item in candidates
            if item['name'] not in keep_names[item['quality']]
        ]
        if not candidates:
            return None

        # 刷新标准只看实际收益，不受符咒类别影响。
        lowest_score = min(item['score'] for item in candidates)
        return random.choice([
            item
            for item in candidates
            if item['score'] == lowest_score
        ])

    def _recognize_grigri_options_with_retry(self) -> list[dict]:
        """三个选项全未知时，用下一张截图复核一次。"""
        options = self._recognize_grigri_options()
        if len(options) == 3 and all(not item['name'] for item in options):
            logger.info(
                'All Chess grigri options unknown; wait for next screenshot '
                'and recognize once more'
            )
            time.sleep(self.ACTION_SETTLE_INTERVAL)
            self.screenshot()
            self._grigri_quality_cache = None
            options = self._recognize_grigri_options()
            if len(options) == 3 and all(not item['name'] for item in options):
                logger.warning(
                    'All Chess grigri options still unknown after recheck; '
                    'allow random selection'
                )
        return options

    def select_grigri(self) -> bool:
        """识别、评分并选择符咒；低分时对目标选项刷新一次。"""
        if not self.appear(self.I_SELECT_GRIGRI):
            return False

        # 每次进入新的选符咒界面，三个位置各允许刷新一次。
        self._grigri_quality_cache = None
        self._grigri_refresh_remaining = [1, 1, 1]
        options = self._recognize_grigri_options_with_retry()
        for _ in range(self.GRIGRI_REFRESH_MAXIMUM):
            refresh_target = self._next_refreshable_grigri_option(
                options
            )
            if refresh_target is None:
                logger.info(
                    'Stop Chess grigri refresh: '
                    f'no refreshable position, '
                    f'remaining={self._grigri_refresh_remaining}'
                )
                break
            if not self._refresh_grigri_option(refresh_target):
                break
            options = self._recognize_grigri_options_with_retry()

        selected = self._best_grigri_option(options)
        selected_rule = selected['rule']
        deadline = time.monotonic() + self.GRIGRI_SELECT_TIMEOUT
        attempts = 0
        logger.debug(
            f'Chess grigri option locked: option={selected["index"]}, '
            f'name={selected["name"] or "未知"}, '
            f'category={selected["category"]}, '
            f'benefit={selected["score"]:g}; '
            'retry until selection panel closes'
        )
        while time.monotonic() < deadline:
            self.screenshot()
            if not self.appear(self.I_SELECT_GRIGRI):
                logger.debug(
                    'Chess grigri selection confirmed: '
                    f'option={selected["index"]}, attempts={attempts}'
                )
                return True
            attempts += 1
            self.click(selected_rule)
            time.sleep(self.ACTION_SETTLE_INTERVAL)

        self.screenshot()
        if not self.appear(self.I_SELECT_GRIGRI):
            return True
        logger.warning(
            'Chess grigri selection did not close after repeated clicks: '
            f'option={selected["index"]}, attempts={attempts}'
        )
        return False

    @staticmethod
    def _normalize_ocr_text(value) -> str:
        if value is None:
            return ''
        return ''.join(str(value).split())

    @classmethod
    def _fuzzy_text_match(
        cls,
        expected,
        current,
    ) -> tuple[bool, float, float]:
        """Chess 内部 OCR 编辑距离兜底，避免修改公共 RuleOcr。"""
        expected = cls._normalize_ocr_text(expected)
        current = cls._normalize_ocr_text(current)
        threshold = 0.75 if len(expected) <= 2 else 0.65
        if not expected or not current:
            return False, 0.0, threshold
        if expected == current:
            return True, 1.0, threshold

        # 仅保留较短字符串长度的一行动态规划状态。
        if len(expected) < len(current):
            expected, current = current, expected
        previous = list(range(len(current) + 1))
        for expected_index, expected_char in enumerate(expected, start=1):
            row = [expected_index]
            for current_index, current_char in enumerate(current, start=1):
                row.append(min(
                    row[current_index - 1] + 1,
                    previous[current_index] + 1,
                    previous[current_index - 1]
                    + (expected_char != current_char),
                ))
            previous = row
        similarity = 1.0 - previous[-1] / max(len(expected), len(current))
        return similarity >= threshold, similarity, threshold

    def _read_detail_frame(self):
        """同一截图只匹配、读取一次；未命中 badge 时不读取范围。"""
        image = self.device.image
        cache = getattr(self, '_detail_frame_cache', None)
        if cache is not None and cache[0] is image:
            return cache[1]
        captured_at = time.monotonic()
        marker = getattr(self, 'I_STAGE_MONSTERNORMAL', self.I_STAGE_MONSTER)
        monster = self.appear(marker)
        if not monster and self._stage_marker_visible(self.I_STAGE_HYAKKI):
            frame = self.F_CHESS_DETAIL_HYAKKI
        else:
            frame = self.F_CHESS_DETAIL_MONSTER if monster else self.F_CHESS_DETAIL_NORMAL
        values = frame.execute(self)
        result = (monster, values)
        self._detail_frame_cache = image, result, captured_at
        return result

    def _is_early_round_layout(self) -> bool:
        monster, values = self._read_detail_frame()
        return monster and values is not None

    def _read_round_number(self) -> int | None:
        _, values = self._read_detail_frame()
        raw = self._normalize_ocr_text(values[0]) if values else ''
        matched = re.fullmatch(r'\d+', raw)
        return int(raw) if matched and int(raw) > 0 else None

    # 阶段文字未知时，使用金色激活图标兜底。
    SPECIAL_STAGE_RULES = (('I_STAGE_HYAKKI', '鬼'),)
    BOSS_STAGE_RULES = ('I_STAGE_ORCHI', 'I_STAGE_DEMON')

    def _stage_marker_visible(self, rule) -> bool:
        """回合预告中的灰色图案也会模板命中，只接受金色激活图案。"""
        x, y, w, h = rule.roi_back
        region = self.device.image[y:y+h, x:x+w]
        template = rule.image
        th, tw = template.shape[:2]
        if region.shape[0] < th or region.shape[1] < tw:
            return False
        scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(scores >= rule.threshold)
        for px, py in zip(xs, ys):
            hsv = cv2.cvtColor(region[py:py+th, px:px+tw], cv2.COLOR_RGB2HSV)
            gold = ((hsv[:, :, 0] >= 10) & (hsv[:, :, 0] <= 45)
                    & (hsv[:, :, 1] >= 100) & (hsv[:, :, 2] >= 150))
            if float(np.mean(gold)) >= .10:
                return True
        return False

    def _special_chess_mode(self) -> str | None:
        for rule_name, mode in self.SPECIAL_STAGE_RULES:
            if self._stage_marker_visible(getattr(self, rule_name)):
                return mode
        return None

    def _read_chess_mode(self) -> str | None:
        """先读 Frame 阶段文字；未知时用金色百鬼图标兜底。"""
        _, values = self._read_detail_frame()
        if values is None:
            return self._special_chess_mode()
        raw = self._normalize_ocr_text(values[1])
        if raw in ('备', '战', '鬼', '待'):
            return raw
        special_mode = self._special_chess_mode()
        if special_mode is not None:
            return special_mode
        # 保留非空未知文字的等待语义，避免误累计结算空帧。
        return '待' if raw else None

    def _is_boss_challenge_round(self, round_no=None) -> bool:
        """以大蛇、逢魔激活标志确定首领回合，切换专用棋盘布局。"""
        key = (self.chess_state.game_number,
               self._current_round_no if round_no is None else round_no)
        if any(self._stage_marker_visible(getattr(self, name))
               for name in self.BOSS_STAGE_RULES):
            self._boss_challenge_round_key = key
            return True
        return getattr(self, '_boss_challenge_round_key', None) == key

    def _board_set_has_shikigami(self, set_index: int) -> bool | None:
        """按当前普通或首领棋盘的 Frame 判断占位。"""
        return self._board_set_has_shikigami_visual(set_index)

    def _board_set_has_shikigami_visual(self, set_index: int) -> bool | None:
        """按表情开关定位的 Frame 判断指定站位是否有人。"""

        index = int(set_index)
        if not 1 <= index <= 12:
            raise ValueError(f'Chess board set position is not configured: {set_index}')
        slots = self._board_frame_scan()
        return bool(slots[index - 1]['occupied']) if slots else None

    def _board_set_is_empty(self, set_index: int) -> bool:
        """普通或首领 Frame 确认原站位为空，锚点缺失不算下阵成功。"""
        index = int(set_index)
        if not 1 <= index <= 12:
            raise ValueError(f'Chess board set position is not configured: {set_index}')
        slots = self._board_frame_scan()
        return bool(slots and not slots[index - 1]['occupied'])

    def _reconcile_preparation_lineup(self, slots) -> list[str]:
        """复用关店后已确认有人的棋盘结果，清理离场式神记录。"""
        deployed_names = set(
            getattr(self, '_board_lineup_names', set())
        )
        actual_positions = dict(
            getattr(self, '_board_actual_positions', {})
        )
        player_positions = set(
            getattr(self, '_player_deployed_positions', set())
        )
        removed = []
        for name in sorted(deployed_names):
            set_index = actual_positions.get(
                name,
                self.shikigami_deploy_positions.get(name),
            )
            if set_index is None:
                continue
            if slots[int(set_index) - 1]['occupied']:
                continue
            deployed_names.discard(name)
            actual_positions.pop(name, None)
            player_positions.discard(int(set_index))
            removed.append(name)
            logger.warning(
                'Chess preparation shikigami missing; '
                f'mark as undeployed: name={name}, set={set_index}'
            )

        self._board_lineup_names = deployed_names
        self._board_actual_positions = actual_positions
        self._player_deployed_positions = player_positions
        logger.info(
            'Chess preparation lineup check: '
            f'removed={removed}, deployed={sorted(deployed_names)}'
        )
        return removed

    def _read_board_position_count(self) -> dict | None:
        """统计 12 个站位勾玉区域中检测到图标的位置数量。"""
        slots = self._board_frame_scan()
        if not slots:
            return None
        occupied_positions = [
            set_index
            for set_index, slot in enumerate(slots, 1)
            if slot['occupied']
        ]
        count = len(occupied_positions)
        raw = ','.join(str(index) for index in occupied_positions)
        logger.debug(
            'Chess board position count by jade: '
            f'{count}/12, occupied={occupied_positions}'
        )
        return {
            'current': count,
            'total': 12,
            'raw': raw,
            'occupied_positions': occupied_positions,
        }

    def _read_shikigami_count(self) -> dict | None:
        """读取场上人数：统计 12 个站位勾玉区域是否有式神。"""
        return self._read_board_position_count()

    def _read_lineup_capacity_status(self) -> dict | None:
        """以本局已确认上阵名单统计人口；金鱼等特殊单位不计人口。"""
        level = self._read_level()
        if level is None:
            logger.warning(
                'Chess lineup capacity unavailable: '
                f'level={level}'
            )
            return None

        deployed_names = {
            name
            for name in getattr(self, '_board_lineup_names', set())
            if name in self.shikigami_deploy_positions
        }
        player_positions = set(
            getattr(self, '_player_deployed_positions', set())
        )
        current = len(deployed_names)
        capacity = level
        full = current >= capacity
        logger.debug(
            'Chess lineup capacity by runtime state: '
            f'population={current}, '
            f'capacity={capacity}, level={level}, '
            f'deployed={sorted(deployed_names)}, '
            f'player_positions={sorted(player_positions)}, '
            f'arakawa_goldfish='
            f'{getattr(self, "_arakawa_goldfish_current_position", None)}, '
            f'full={full}'
        )
        return {
            'current': current,
            'capacity': capacity,
            'level': level,
            'deployed_names': deployed_names,
            'player_positions': player_positions,
            'arakawa_goldfish': (
                getattr(self, '_arakawa_goldfish_current_position', None)
                is not None
            ),
            'full': full,
        }

    def _read_level(self) -> int | None:
        """读取“一阶”至“九阶”，同时兼容阿拉伯数字显示。"""
        raw = self._normalize_ocr_text(self.O_LEVEL.ocr(self.device.image))
        digit = re.search(r'([1-9])', raw)
        if digit is not None:
            level = int(digit.group(1))
            logger.debug(f'Chess level: [{raw}] -> {level}')
            return level

        chinese_digits = {
            '一': 1,
            '二': 2,
            '三': 3,
            '四': 4,
            '五': 5,
            '六': 6,
            '七': 7,
            '八': 8,
            '九': 9,
        }
        for character, level in chinese_digits.items():
            if character in raw:
                logger.debug(f'Chess level: [{raw}] -> {level}')
                return level

        logger.warning(f'Chess level OCR invalid: [{raw}]')
        return None

    def _read_remaining_time(self) -> int | None:
        """读取当前 Frame 的第三个范围，复用当前截图。"""
        _, values = self._read_detail_frame()
        raw = self._normalize_ocr_text(values[2]) if values else ''
        return int(raw) if re.fullmatch(r'\d+', raw) else None

    def _read_game_rank(self) -> tuple[int | None, str]:
        """读取结算页“第几名”，兼容阿拉伯数字和中文数字。"""
        raw = self._normalize_ocr_text(self.O_RANK.ocr(self.device.image))
        matched = re.search(r'第?([1-8])名?', raw)
        if matched is not None:
            return int(matched.group(1)), raw

        chinese_digits = {
            '一': 1,
            '二': 2,
            '三': 3,
            '四': 4,
            '五': 5,
            '六': 6,
            '七': 7,
            '八': 8,
        }
        for character, rank in chinese_digits.items():
            if character in raw:
                return rank, raw
        return None, raw
