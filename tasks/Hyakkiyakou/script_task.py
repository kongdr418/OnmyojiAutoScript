# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time

from datetime import datetime, timedelta
from cached_property import cached_property

from module.exception import TaskEnd
from module.logger import logger
from module.exception import RequestHumanTakeover
from tasks.Component.SwitchOnmyoji.switch_onmyoji import SwitchOnmyoji
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_hyakkiyakou, page_main, page_onmyodo
from tasks.Hyakkiyakou.config import InferenceEngine, ModelPrecision
from tasks.Hyakkiyakou.agent.agent import Agent
from tasks.Hyakkiyakou.slave.hya_slave import HyaSlave
from module.hyakkiyakou import Debugger
from module.hyakkiyakou.labels import CLASSINDEX as CI, MODEL_BOSS_SCORES, id2name, label2id
from module.hyakkiyakou.tracker import Tracker, draw_tracks


class ScriptTask(GameUi, HyaSlave, SwitchOnmyoji):

    @property
    def _config(self):
        return self.config.hyakkiyakou

    @cached_property
    def tracker(self) -> Tracker:
        hyakkiyakou_models = self._config.hyakkiyakou_models
        conf = hyakkiyakou_models.conf_threshold
        nms = hyakkiyakou_models.iou_threshold
        if conf < 0.2 or conf > 1:
            raise RequestHumanTakeover('conf_threshold should be in [0.2, 1]')
        if nms < 0.2 or nms > 1:
            raise RequestHumanTakeover('iou_threshold should be in [0.2, 1]')
        inf_en = 'onnxruntime' if hyakkiyakou_models.inference_engine == InferenceEngine.ONNXRUNTIME else 'tensorrt'
        precision = 'fp32' if hyakkiyakou_models.model_precision == ModelPrecision.FP32 else 'int8'
        # 这个坑后面再补
        if inf_en == 'tensorrt' or precision == 'int8':
            raise RequestHumanTakeover('Only support onnxruntime and FP32 now')
        return Tracker(args={'conf_threshold': conf, 'iou_threshold': nms})

    @cached_property
    def agent(self) -> Agent:
        hya_config = self._config.hyakkiyakou_config
        sp = hya_config.hya_sp
        ssr = hya_config.hya_ssr
        sr = hya_config.hya_sr
        r = hya_config.hya_r
        n = hya_config.hya_n
        g = hya_config.hya_g
        weights: list = [sp, ssr, sr, r, n, g]
        str_priorities: str = hya_config.hya_priorities
        if str_priorities == '':
            priorities = []
        else:
            str_priorities = str_priorities.replace(' ', '').replace('，', ',')
            try:
                priorities = [label2id(s) for s in str_priorities.split(',')]
            except Exception as e:
                logger.error(f'Priority error: {str_priorities}')
                logger.error(e)
                raise RequestHumanTakeover
        strategy: dict = {
            'weights': weights,
            'priorities': priorities,
            'invite_friend': hya_config.hya_invite_friend,
            'auto_bean': hya_config.hya_auto_bean,
            'bean_threshold_low': hya_config.hya_bean_threshold_low,
            'buff_omega': {
                'prob_up': hya_config.hya_buff_prob_up,
                'speed_up': hya_config.hya_buff_speed_up,
                'add_beans': hya_config.hya_buff_add_beans,
                'slow_down': hya_config.hya_buff_slow_down,
                'freeze': hya_config.hya_buff_freeze,
            }
        }
        return Agent(strategy=strategy)

    @cached_property
    def debugger(self) -> Debugger:
        debug_config = self._config.debug_config
        hya_interval = debug_config.hya_interval
        hya_save_result = debug_config.hya_save_result
        if hya_interval <= 100 or hya_interval >= 1000:
            raise RequestHumanTakeover('screenshot_interval must be between 1000 and 10000')
        self.set_fast_screenshot_interval(hya_interval)
        return Debugger(info_enable=debug_config.hya_info, 
                        continuous_learning=debug_config.continuous_learning,
                        hya_save_result=hya_save_result)

    def run(self):
        hya_count: int = 0
        self.limit_count: int = self._config.hyakkiyakou_config.hya_limit_count
        limit_time = self._config.hyakkiyakou_config.hya_limit_time
        self.limit_time: timedelta = timedelta(hours=limit_time.hour, minutes=limit_time.minute,
                                               seconds=limit_time.second)
        self.tracker.prepare()
        self.goto_page(page_onmyodo)
        self.switch_onmyoji(self._config.hyakkiyakou_config.hya_onmyoji)
        self.goto_page(page_hyakkiyakou)

        while 1:
            if hya_count >= self.limit_count:
                logger.info('Hyakkiyakou count limit out')
                break
            if datetime.now() - self.start_time >= self.limit_time:
                logger.info('Hyakkiyakou time limit out')
                break

            self.one()
            hya_count += 1
            logger.info(f'count: {hya_count}/{self.limit_count}')
            logger.info(f'time: {(datetime.now() - self.start_time).total_seconds():.1f}s/{self.limit_time.total_seconds()}s')

        while 1:
            self.screenshot()

            if not self.appear(self.I_HACCESS):
                continue
            if self.appear(self.I_HCLOSE_RED):
                break
        self.ui_click_until_disappear(self.I_HCLOSE_RED)
        self.set_next_run(task='Hyakkiyakou', success=True, finish=False)
        raise TaskEnd

    # ------------ 新增三个函数：用于检测 ------------
    def _rarity_score(self, class_id: int) -> int:
        """
        返回一个用于比较的稀有度分数：
        SP > SSR > SR > R > N > G
        没有匹配到的返回 -1
        """
        if class_id in MODEL_BOSS_SCORES:
            return MODEL_BOSS_SCORES[class_id]
        # G（呱太）
        if CI.MIN_G <= class_id <= CI.MAX_G:
            return 0
        # N
        if CI.MIN_N <= class_id <= CI.MAX_N:
            return 1
        # R（排除童男/童女）
        if (CI.MIN_R <= class_id <= CI.MAX_R) and class_id not in (CI.R_007, CI.R_008):
            return 2
        # SR
        if CI.MIN_SR <= class_id <= CI.MAX_SR:
            return 3
        # SSR
        if CI.MIN_SSR <= class_id <= CI.MAX_SSR:
            return 4
        # SP
        if CI.MIN_SP <= class_id <= CI.MAX_SP:
            return 5
        # 其他（buff/未收录 等）不参与排序
        return -1

    def _detect_current_rarity(self) -> tuple[int, int]:
        """
        在当前截图上跑一遍 tracker，返回：
        (最高稀有度分数, 对应的 class_id)
        如果没检测到式神，则返回 (-1, -1)
        """
        self.screenshot()
        detections = self.tracker.detect(self.device.image, include_initial=True)
        best_score = -1
        best_class = -1
        for label, _, _ in detections:
            score = self._rarity_score(label)
            if score > best_score:
                best_score = score
                best_class = label
        if best_class != -1:
            logger.info(
                f'Hyakki select: detect {id2name(best_class)} '
                f'with rarity score {best_score}'
            )
        else:
            logger.warning('Hyakki select: no valid shikigami detected on title screen')
        return best_score, best_class

    def _select_best_boss(self):
        """
        在鬼王选择界面依次点三个候选，识别稀有度，最终选择最高稀有度的那个。
        要求：当前已经在 I_HTITLE 画面。
        """
        candidates = [self.C_HSELECT_1, self.C_HSELECT_2, self.C_HSELECT_3]
        logger.info('Start selecting boss')
        best_idx = 0
        best_score = -1
        scores = []

        for idx, btn in enumerate(candidates):
            # 点一下第 idx 个候选，让它成为当前选中的式神
            self.click(btn, interval=0.1)
            time.sleep(1)  # 给界面一点刷新时间
            score, cls = self._detect_current_rarity()
            scores.append(score)
            if score > best_score:
                best_score = score
                best_idx = idx
        
        #所以如果三个都识别失败了会选第一个
        logger.info(f'Hyakki select scores: {scores}, choose index {best_idx + 1}') 

        # 记录一下，后面如果需要兜底再点一次可以用
        self._best_boss_button = candidates[best_idx]

        # 再点一次最高稀有度那个，确保它处于选中状态
        self.click(self._best_boss_button, interval=0.1)
        time.sleep(0.5)

    def one(self):
        self.reset_state()
        self.goto_page(page_hyakkiyakou)
        self.screenshot()
        if not self.appear(self.I_HACCESS):
            logger.error('Failed to navigate to Hyakkiyakou page after 3 retries')
            raise RequestHumanTakeover('Failed to navigate to Hyakkiyakou page')
        if self._config.hyakkiyakou_config.hya_invite_friend:
            self.invite_friend()
        # start
        self.ui_click(self.I_HACCESS, self.I_HSTART, interval=2)
        self.wait_until_appear(self.I_HTITLE)
        # 这里改成：在三个候选中选择稀有度最高的作为鬼王
        self._best_boss_button = None
        self._select_best_boss()
        while 1:
            self.screenshot()
            if not self.appear(self.I_HTITLE):
                break
            if self.appear_then_click(self.I_HSTART, interval=2):
                continue
            if not self.appear(self.I_HSELECTED) and getattr(self, '_best_boss_button', None) is not None:
                self.click(self._best_boss_button, interval=2)  # 保险：如果因为某些原因还没处于“已选中”状态，就再点一次最佳按钮
                continue
        self.device.stuck_record_add('BATTLE_STATUS_S')
        # 正式开始
        logger.hr('Start Hyakkiyakou')
        last_action = [0, 0, False, 10]
        self.hya_fs_check_timer.reset()
        if self._config.debug_config.hya_show:
            self.debugger.show_start()
        while 1:
            self.fast_screenshot(screenshot=self._config.debug_config.hya_screenshot_method)
            if self.appear(self.I_HEND):
                break
            if not self.appear(self.I_CHECK_RUN):
                continue
            #修改：在这里不再区分freeze，而是将状态传到decision用于执行冻结策略
            #目前被禁用了 因为冰冻状态下检测正确率约等于0 全是蝉冰雪女 =.=
            if not self.appear(self.I_HFREEZE):
                # -------------------------------------------------------
                freeze = self.appear(self.I_HFREEZE)
                self.slave_state = self.update_state()
                tracks = self.tracker(image=self.device.image, response=last_action)
                last_action = self.agent.decision(tracks=tracks, state=self.slave_state, freeze=freeze)
                self.do_action(last_action, state=self.slave_state)
            else:
                # TODO freeze state
                tracks = []

            # debug
            if self._config.debug_config.hya_show:
                draw_image = draw_tracks(self.device.image, tracks=tracks)
                self.debugger.show_sync(image=draw_image)
            if self._config.debug_config.continuous_learning:
                self.debugger.deal_learning(image=self.device.image, tracks=tracks)
            if self._config.debug_config.hya_info:
                self.debugger.show_info(tracker=self.tracker, f=self.agent.focus)

        logger.info('Hyakkiyakou End')
        if self._config.debug_config.hya_show:
            self.debugger.show_stop()
        if self._config.debug_config.hya_save_result:
            # 走个动画 - 修改了时间以保证能截到图
            time.sleep(2)
            self.debugger.save_result(self.device.image)
        self.ui_click(self.I_HEND, self.I_HACCESS)
        self.debugger.save_images()
        del self.debugger
        self.tracker.clear_tracks()

    def do_action(self, action: list, state):
        x, y, throw, bean = action
        if not throw or not (0 <= x < 1280 and 0 <= y < 720):
            return
        if state[0] <= 0:
            return
        if state[2] != bean:  # 当前撒豆数量不等于要撒的数量则切换
            if state[2] == 10 and bean == 5:
                self.bean_10to05()
            elif state[2] == 5 and bean == 10:
                self.bean_05to10()
        self.fast_click(x=x, y=y, control_method=self._config.debug_config.hya_control_method)


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)

    t = ScriptTask(c, d)
    t.run()

    # from tasks.Hyakkiyakou.agent.tagent import test_observe, test_state
    # test_state()
    # from debugger import test_track
    # test_track(show=False)
    pass
