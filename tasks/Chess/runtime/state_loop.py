"""Chess 状态驱动回合循环：沿用原动作，集中观察与单局状态。"""

import time

from module.exception import GameStuckError
from module.logger import logger
from tasks.Chess.runtime.decision import ChessAction, decide_round
from tasks.Chess.runtime.events import ChessEventKind, ChessEventMonitor
from tasks.Chess.runtime.state import ChessGameState
from tasks.Chess.runtime.cache import same_frame


class ChessStateLoopMixin:
    def __init__(self, config, device) -> None:
        self.chess_state = ChessGameState()
        self.chess_events = ChessEventMonitor()
        super().__init__(config=config, device=device)

    def _record_observation(self, **values) -> None:
        image = getattr(self.device, 'image', None)
        if image is not None:
            frame_id = getattr(self.device, 'image_frame_id', None)
            self.chess_state.observe(frame_id or id(image), **values)

    def _read_current_health(self) -> int | None:
        """首次百鬼后，在每回合首个确认关店画面读取本人生命。"""
        import re
        import numpy as np
        from module.atom.ocr import RuleOcr

        state = self.chess_state
        round_no = self._read_round_number()
        if (not state.player_name or state.first_hyakki_round is None
                or round_no is None or round_no <= state.first_hyakki_round):
            return None
        if state.health_read_round == round_no:
            return state.current_health
        if self._shop_assumed_open or self._shop_refresh_marker_visible():
            return state.current_health
        state.health_read_round = round_no
        image = self.device.image
        matches = []
        for result in self.O_NAME_RANGE.detect_and_ocr(image):
            text = self._normalize_ocr_text(result.ocr_text)
            if text != state.player_name:
                continue
            points = np.asarray(result.box)
            right, bottom = points.max(axis=0)
            right += self.O_NAME_RANGE.roi[0]
            bottom += self.O_NAME_RANGE.roi[1]
            matches.append((int(right), int(bottom)))
        if len(matches) == 1:
            right, bottom = matches[0]
            left, top = max(0, right - 36), max(0, bottom)
            width = min(image.shape[1], right + 6) - left
            height = min(image.shape[0], top + 28) - top
            if width > 0 and height > 0:
                roi = (left, top, width, height)
                rule = RuleOcr(name='chess_current_health', roi=roi, area=roi,
                               mode='Digit', method='Default', keyword='')
                raw = self._normalize_ocr_text(rule.ocr(image))
                if re.fullmatch(r'\d{1,3}', raw) and 0 <= int(raw) <= 100:
                    state.current_health = int(raw)
        self._record_observation(player_name=state.player_name,
                                 current_health=state.current_health)
        logger.info(f'Chess player health: round={round_no}, '
                    f'name={state.player_name}, health={state.current_health}')
        return state.current_health

    def _ensure_shop_closed(self, allowed_modes=('备',)) -> bool:
        closed = super()._ensure_shop_closed(allowed_modes=allowed_modes)
        if closed:
            self._inspect_closed_shop_once()
        return closed

    def _inspect_closed_shop_once(self, round_no: int | None = None) -> None:
        """生命和下阵共用关店确认帧；检测过程不截图、不操作商店。"""
        observed_round = self._read_round_number()
        if observed_round is None or (round_no is not None and observed_round != round_no):
            return
        if (self._shop_assumed_open or self._shop_refresh_marker_visible()
                or self.appear(self.I_SELECT_GRIGRI)):
            return
        image = self.device.image
        key = (getattr(self.device, 'image_frame_id', None), id(image), observed_round)
        previous = getattr(self, '_closed_shop_inspection_frame', None)
        if previous is not None and previous[0] == key:
            return
        self._closed_shop_inspection_frame = (key, image)
        self._read_current_health()
        self._check_preparation_lineup_once(observed_round)

    def _read_round_number(self) -> int | None:
        value = super()._read_round_number()
        self._record_observation(round_no=value)
        return value

    def _read_chess_mode(self) -> str | None:
        value = super()._read_chess_mode()
        self._record_observation(mode=value)
        if not hasattr(self, '_logged_chess_mode') or self._logged_chess_mode != value:
            self._logged_chess_mode = value
            logger.info(
                f'Chess phase detected: mode={value or "未识别"}, '
                f'round={getattr(self, "_current_round_no", None)}'
            )
        return value

    @same_frame
    def _read_level(self) -> int | None:
        value = super()._read_level()
        self._record_observation(level=value)
        return value

    @same_frame
    def _read_shop_gold(self) -> int | None:
        value = super()._read_shop_gold()
        self._record_observation(gold=value)
        return value

    def _read_remaining_time(self) -> int | None:
        value = super()._read_remaining_time()
        self._record_observation(remaining_seconds=value)
        return value

    def _is_in_chess_game(self) -> bool:
        value = super()._is_in_chess_game()
        self._record_observation(in_game=value)
        return value

    def _shop_refresh_marker_visible(self) -> bool:
        value = super()._shop_refresh_marker_visible()
        self._record_observation(shop_refresh_visible=value)
        return value

    def _read_game_rank(self) -> tuple[int | None, str]:
        value, raw = super()._read_game_rank()
        self._record_observation(game_rank=value)
        return value, raw

    def _clear_player_identity(self) -> None:
        """本人名字和生命仅供当局使用，结束或中断后立即清理。"""
        from dataclasses import replace

        state = self.chess_state
        state.player_name = None
        state.first_hyakki_round = None
        state.health_read_round = None
        state.current_health = None
        if state.observation is not None:
            state.observation = replace(state.observation,
                                        player_name=None, current_health=None)
        self._hyakki_player_row = None
        self._hyakki_player_found = False

    def _run_round_loop(self) -> None:
        try:
            super()._run_round_loop()
        finally:
            self._clear_player_identity()

    def _start_chess_game(self) -> None:
        self._clear_player_identity()
        self.chess_state.begin_game()
        self._detail_frame_cache = None
        self._chess_reader_cache = None
        self._unchanged_store_cache = None
        self._closed_shop_inspection_frame = None
        self.chess_events.reset()
        logger.info(f'Chess state: begin game {self.chess_state.game_number}')
        super()._start_chess_game()

    def _publish_chess_events(self, *, decision=None,
                              grigri_visible=False, game_ended=False):
        events = self.chess_events.collect(
            self.chess_state.observation,
            decision=decision,
            grigri_visible=grigri_visible,
            game_ended=game_ended,
        )
        for event in events:
            logger.debug(
                f'Chess event: {event.kind.value}, '
                f'round={event.round_no}, mode={event.mode}, '
                f'frame={event.frame_id}'
            )
        return events

    def _check_preparation_lineup_once(self, round_no: int) -> None:
        """只检查现有关闭商店截图；有效有人棋盘每回合核对一次。"""
        checked = self.chess_state.preparation_lineup_checked_rounds
        if round_no in checked:
            return
        if (self._read_round_number() != round_no
                or self._read_chess_mode() != '备'
                or self.appear(self.I_SELECT_GRIGRI)):
            return
        if (self._shop_refresh_marker_visible()
                or self._shop_assumed_open):
            return
        slots = self._board_frame_scan(round_no=round_no)
        if not slots or not any(slot['occupied'] for slot in slots):
            return
        self._reconcile_preparation_lineup(slots)
        checked.add(round_no)

    def run_one_round(self, round_no: int) -> int | None:
        """观察→决策→同步执行；原动作及优先级不变。"""
        progress = self.chess_state.begin_round(round_no)
        self._current_round_no = int(round_no)
        logger.debug(f'Chess round {round_no}')

        while True:
            self.device.stuck_record_clear()
            self.screenshot()
            if self.appear(self.I_SELECT_GRIGRI):
                self._record_observation(grigri_visible=True)
                self._publish_chess_events(grigri_visible=True)
                logger.info(
                    'Chess round-state refresh interrupted by grigri selection; '
                    'resolve grigri before reading round and mode'
                )
                self.select_grigri()
                time.sleep(self.SLOW_POLL_INTERVAL)
                continue
            self._record_observation(grigri_visible=False)
            if self._finish_chess_game_after_markers_missing(
                f'round_{round_no}'
            ):
                self._publish_chess_events(game_ended=True)
                return None
            if getattr(self, '_rank_protection_exit_requested', False):
                logger.info(
                    'Chess rank protection: actively exit this game; '
                    'the game will not count toward completed runs'
                )
                if self.Chess_Exit(return_to_lobby=False):
                    self._rank_protection_exit_succeeded = True
                    self._publish_chess_events(game_ended=True)
                    return None
                logger.warning(
                    'Chess rank-protection exit was unavailable; '
                    'retry on the next state refresh'
                )
            observed_round = self._read_round_number()
            mode = self._read_chess_mode()
            self._read_remaining_time()
            if observed_round == round_no and mode is not None:
                self._inspect_closed_shop_once(round_no)
            round_transition_pending = (
                observed_round is not None and observed_round != round_no
            )
            # 首帧回目变化时立即冻结旧回目的后续检测和动作。
            in_game = True if round_transition_pending else (
                mode is not None or self._is_in_chess_game()
            )
            grigri_visible = (
                self.appear(self.I_SELECT_GRIGRI)
                if not round_transition_pending and mode == '备'
                else False
            )
            self._record_observation(
                in_game=in_game, grigri_visible=grigri_visible,
            )
            decision = decide_round(
                self.chess_state.observation,
                progress,
                now=time.monotonic(),
                confirm_frames=self.ROUND_CONFIRM_FRAMES,
                unknown_timeout=self.UNKNOWN_STATE_TIMEOUT,
                in_game=in_game,
                grigri_visible=grigri_visible,
            )
            progress = decision.progress
            self.chess_state.round = progress
            events = self._publish_chess_events(
                decision=decision, grigri_visible=grigri_visible,
            )

            if any(event.kind is ChessEventKind.ROUND_CONFIRMED for event in events):
                logger.debug(
                    f'Chess round boundary confirmed: {round_no} -> '
                    f'{observed_round}, phase={progress.phase}, '
                    f'preparation_done={progress.preparation_done}'
                )
                return observed_round

            if decision.action is ChessAction.ROUND_PENDING:
                continue
            if decision.action is ChessAction.LOST_MARKERS:
                raise GameStuckError(
                    f'Chess: lost all markers during round {round_no}'
                )

            if decision.action is ChessAction.RESOLVE_GRIGRI:
                logger.debug('Chess preparation interrupted by grigri selection')
                self.select_grigri()
                continue

            if decision.action in (ChessAction.BATTLE, ChessAction.PASSIVE):
                if mode == '鬼' and not progress.hyakki_round_seen:
                    progress.hyakki_round_seen = True
                    logger.info(
                        f'Chess Hyakki mode observed: round={round_no}'
                    )
                if decision.action is ChessAction.BATTLE:
                    if not progress.battle_hand_cleanup_done:
                        progress.battle_hand_cleanup_done = (
                            self._handle_battle_sell_stage()
                        )
                        progress.battle_hand_cleanup_status = self.chess_state.hand_cleanup_status
                    if (
                        progress.battle_hand_cleanup_done
                        and not progress.battle_economy_done
                        and self._read_chess_mode() == '战'
                    ):
                        self._run_battle_economy_until_budget_limit()
                        progress.battle_economy_done = True
                    self._handle_passive_stage('战')
                else:
                    self._handle_passive_stage(mode)
                if progress.phase == 'await_preparation':
                    progress.preparation_done = True
                    progress.phase = 'await_battle_end'
                    logger.warning(
                        f'Chess round {round_no}: resumed in passive mode '
                        f'{mode}; treat preparation as already passed'
                    )

            elif decision.action is ChessAction.PREPARE:
                if progress.phase == 'await_preparation':
                    # Share this closed-shop view between health and lineup checks.
                    # Read this round's health before choosing the gold reserve.
                    if (self.chess_state.player_name
                            and self.chess_state.first_hyakki_round is not None
                            and round_no > self.chess_state.first_hyakki_round
                            and self.chess_state.health_read_round != round_no):
                        if not self._ensure_shop_closed():
                            continue
                    self.purchase_lineup_cards_once()
                    self._run_preparation_economy_until_time_limit()
                    if not self._is_preparation_mode():
                        time.sleep(self.SLOW_POLL_INTERVAL)
                        continue
                    if self._handle_preparation_stage(1):
                        progress.preparation_done = True
                        progress.phase = 'await_battle'
                        logger.debug(
                            f'Chess round {round_no}: preparation '
                            'complete; wait for 战/鬼/待'
                        )

            # 下一轮截图由设备的 combat 截图限速控制，不叠加固定轮询等待。


def _game_state_property(name):
    """让旧执行方法读写同一份状态，避免维护两套可变副本。"""
    return property(
        lambda self: getattr(self.chess_state, name),
        lambda self, value: setattr(self.chess_state, name, value),
    )


_GAME_STATE_FIELDS = {
    '_economy_pending': 'economy_pending',
    '_economy_step_state': 'economy_step_state',
    '_economy_sequence_level': 'economy_sequence_level',
    '_economy_sequence_index': 'economy_sequence_index',
    '_economy_battle_mode': 'economy_battle_mode',
    '_shop_assumed_open': 'shop_assumed_open',
    '_board_lineup_names': 'board_lineup_names',
    '_board_actual_positions': 'board_actual_positions',
    '_player_deployed_positions': 'player_deployed_positions',
    '_board_shikigami_attributes': 'board_shikigami_attributes',
    '_board_special_units': 'board_special_units',
    '_arakawa_goldfish_current_position': 'arakawa_goldfish_current_position',
    '_last_game_rank': 'last_game_rank',
}
for _legacy_name, _state_name in _GAME_STATE_FIELDS.items():
    setattr(ChessStateLoopMixin, _legacy_name, _game_state_property(_state_name))
