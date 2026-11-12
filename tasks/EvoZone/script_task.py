# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from time import sleep
from datetime import time, datetime, timedelta

from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralInvite.general_invite import GeneralInvite
from tasks.Component.GeneralBuff.general_buff import GeneralBuff
from tasks.Component.GeneralRoom.general_room import GeneralRoom
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_awake_zones, page_shikigami_records
from tasks.EvoZone.assets import EvoZoneAssets
from tasks.EvoZone.config import EvoZone, UserStatus, KirinType
from tasks.Component.GeneralBattle.battle_wait import battle_wait_options, battle_wait_strategy

from module.atom.click import RuleClick
from module.logger import logger
from module.exception import TaskEnd


class EvoZoneNoClickAreas:
    """
    结算 / 胜利界面上人工标注的「禁点区」（1280x720）。

    2026-11-12 由用户提供 6 张标注截图（结算界面、结算界面2、胜利界面，
    各有实心填充与空心框两版）归纳而来。红框内是奖励物品、式神立绘、
    式神经验卡、系统消息、buff 倒计时等 UI 控件，兜底随机点击落在上面会
    把界面点走或弹出无关窗口；红框之外的空白才是可以点的位置。

    坐标为三组标注的并集，取整后略作扩张（用户说明手绘不必太精确）：
        左上图标     三图 (8,12)-(141,125) / (32,27)-(119,112) / (17,19)-(134,118)
        右上消息     三图 (610,7)-(1262,76) / (642,0)-(1277,74) / (796,13)-(1272,70)
        中央主区     结算 (220,137)-(1068,507)，胜利另有 (307,127)-(1060,636)
        左下 buff    三图 (110,613)-(462,706) / (106,608)-(487,709) / (136,633)-(468,693)

    assets.py 由 assets_extract.py 自动生成、不可手工修改，故这些 EvoZone
    专属禁区放在这里，由 reward_exclude_click 通过 getattr(self, name) 取用。
    """
    C_EVO_NOCLICK_LEFT_TOP = RuleClick(
        roi_front=(0, 0, 160, 135), roi_back=(0, 0, 160, 135), name='evo_noclick_left_top')
    C_EVO_NOCLICK_RIGHT_TOP = RuleClick(
        roi_front=(600, 0, 680, 80), roi_back=(600, 0, 680, 80), name='evo_noclick_right_top')
    # 下沿取 y=640 而非三图空心框的 602：胜利界面填充版把中央框画到了 636，
    # 留这段余量才不会在 (490,610)-(909,636) 这条横带上漏点。
    C_EVO_NOCLICK_CENTER = RuleClick(
        roi_front=(210, 120, 870, 520), roi_back=(210, 120, 870, 520), name='evo_noclick_center')
    C_EVO_NOCLICK_LEFT_BOTTOM = RuleClick(
        roi_front=(100, 605, 390, 110), roi_back=(100, 605, 390, 110), name='evo_noclick_left_bottom')


class ScriptTask(GeneralBattle, GeneralInvite, GeneralBuff, GeneralRoom, GameUi,
                 EvoZoneAssets, EvoZoneNoClickAreas, SwitchSoul):

    def run(self) -> bool:

        limit_count = self.config.evo_zone.evo_zone_config.limit_count
        limit_time = self.config.evo_zone.evo_zone_config.limit_time
        self.current_count = 0
        self.limit_count: int = limit_count
        self.limit_time: timedelta = timedelta(hours=limit_time.hour, minutes=limit_time.minute,
                                               seconds=limit_time.second)
        con = self.config.evo_zone
        if con.switch_soul_config.enable:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul(con.switch_soul_config.switch_group_team)
        if con.switch_soul_config.enable_switch_by_name:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul_by_name(con.switch_soul_config.group_name, con.switch_soul_config.team_name)

        self.goto_page(page_main)
        config: EvoZone = self.config.evo_zone
        if config.evo_zone_config.soul_buff_enable:
            self.open_buff()
            self.awake(is_open=True)
            self.close_buff()

        success = True
        # 战斗收尾必须确认真的离开了结算页。
        # 只靠 success hook 看到奖励图标消失就结束，结算页残留时 battle_wait
        # 会提前返回，run_leader 接着在错误的页面上静默空转直到 GameStuckError。
        #
        # check_imgs 要列全「战斗结束后所有合法的落地界面」。只要其中一个出现，
        # completion hook 就立即结束并把控制权交回 run_leader / run_member；
        # 都没出现时才会启动兜底随机点击。漏掉合法界面会让兜底点击落到本该由
        # 任务逻辑处理的界面上：队长首次通关后的「发送邀请 / 默认邀请」弹窗会被
        # 点掉，check_and_invite 因此看不到 I_UI_CONFIRM、默认邀请永远勾不上；
        # 队员被遣回大厅等待邀请时则会在大厅里持续乱点。
        _, battle_options = self.loadout_default()
        battle_options['completion'].check_imgs = [
            self.I_UI_CONFIRM,   # 首次通关后的「发送邀请」弹窗，交给 check_and_invite 勾选默认邀请
            self.I_GI_EMOJI_1,   # 组队房间左下表情按钮
            self.I_GI_SPEAK,     # 没有表情按钮的房间用发言按钮
            self.I_GI_IN_ROOM,   # 协战房间左上角标志
            self.I_FORM_TEAM,    # 觉醒副本选层界面（单人 / 房间已散）
            self.I_GI_HOME,      # 队员被遣回庭院 / 大厅，安静等待队长再次邀请
            self.I_GI_EXPLORE,   # 同上，停在探索界面
        ]
        # 兜底点击的取点范围：整屏减去人工标注的禁区。
        # 旧的 SAFE_REWARD_CLICK_AREA=(248,143,815,460) 几乎整块落在中央禁区里，
        # 继续沿用它等于必然点到奖励/立绘，所以 completion 单独放开到整屏，
        # 再由 excludes 精确挖掉 4 块禁区；其余空白处（左右两侧、底部中间、
        # 顶部中间）保持可点，符合「禁区之外都可以点」的标注。
        # 注意：只影响 completion 的兜底点击，success 领奖点击仍使用
        # SAFE_REWARD_CLICK_AREA（OptionSuccessDefault.click_area 未改）。
        battle_options['completion'].click_area = (0, 0, 1280, 720)
        battle_options['completion'].excludes = [
            'C_EVO_NOCLICK_LEFT_TOP',
            'C_EVO_NOCLICK_RIGHT_TOP',
            'C_EVO_NOCLICK_CENTER',
            'C_EVO_NOCLICK_LEFT_BOTTOM',
        ]
        with battle_wait_options(**battle_options):
            match config.evo_zone_config.user_status:
                case UserStatus.LEADER:
                    success = self.run_leader()
                case UserStatus.MEMBER:
                    success = self.run_member()
                case UserStatus.ALONE:
                    self.run_alone()
                case UserStatus.WILD:
                    self.run_wild()
                case _:
                    logger.error('Unknown user status')

        # 记得关掉
        if config.evo_zone_config.soul_buff_enable:
            self.open_buff()
            self.awake(is_open=False)
            self.close_buff()
        # 下一次运行时间
        if success:
            self.set_next_run('EvoZone', finish=True, success=True)
        else:
            self.set_next_run('EvoZone', finish=False, success=False)

        raise TaskEnd

    def evozone_enter(self) -> bool:
        logger.info('Enter evozone')
        kirintype = self.I_LIGHTNING_KIRIN
        match self.config.evo_zone.evo_zone_config.kirin_type:
            case KirinType.FIREKIRIN:
                kirintype = self.I_FIRE_KIRIN
            case KirinType.WINDKIRIN:
                kirintype = self.I_WIND_KIRIN
            case KirinType.WATERKIRIN:
                kirintype = self.I_WATER_KIRIN
            case KirinType.LIGHTNINGKIRIN:
                kirintype = self.I_LIGHTNING_KIRIN
        while True:
            self.screenshot()
            if self.appear(self.I_FORM_TEAM):
                return True
            if self.appear_then_click(kirintype, interval=1):
                continue

    def check_layer(self, layer: str) -> bool:
        """
        检查挑战的层数, 并选中挑战的层
        :return:
        """
        pos = self.list_find(self.L_LAYER_LIST, layer)
        if pos:
            self.device.click(x=pos[0], y=pos[1])
            return True

    def check_lock(self, lock: bool = True) -> bool:
        """
        检查是否锁定阵容, 要求在觉醒界面
        :param lock:
        :return:
        """
        logger.info('Check lock: %s', lock)
        if lock:
            while 1:
                self.screenshot()
                if self.appear(self.I_EVOZONE_LOCK):
                    return True
                if self.appear_then_click(self.I_EVOZONE_UNLOCK, interval=1):
                    continue
        else:
            while 1:
                self.screenshot()
                if self.appear(self.I_EVOZONE_UNLOCK):
                    return True
                if self.appear_then_click(self.I_EVOZONE_LOCK, interval=1):
                    continue

    def run_leader(self):
        logger.info('Start run leader')
        # self.ui_goto(page_soul_zones)
        self.goto_page(page_awake_zones)
        self.evozone_enter()
        layer = self.config.evo_zone.evo_zone_config.layer
        logger.info("test0")
        self.check_layer(layer)
        logger.info("test1")
        self.check_lock(self.config.evo_zone.general_battle_config.lock_team_enable)
        logger.info("test2")
        # 创建队伍
        logger.info('Create team')
        while 1:
            self.screenshot()
            if self.appear(self.I_CHECK_TEAM):
                break
            if self.appear_then_click(self.I_FORM_TEAM, interval=1):
                continue
        # 创建房间
        self.create_room()
        self.ensure_private()
        self.create_ensure()

        # 邀请队友
        success = True
        is_first = True
        # 这个时候我已经进入房间了哦
        while 1:
            self.screenshot()
            # 无论胜利与否, 都会出现是否邀请一次队友
            # 区别在于，失败的话不会出现那个勾选默认邀请的框
            if self.check_and_invite(self.config.evo_zone.invite_config.default_invite):
                continue

            # 检查猫咪奖励
            if self.appear_then_click(self.I_PET_PRESENT, action=self.C_WIN_3, interval=1):
                continue

            if self.current_count >= self.limit_count:
                if self.is_in_room():
                    logger.info('EvoZone count limit out')
                    break

            if datetime.now() - self.start_time >= self.limit_time:
                if self.is_in_room():
                    logger.info('EvoZone time limit out')
                    break

            # 如果没有进入房间那就不需要后面的邀请
            if not self.is_in_room():
                # 如果在探索界面或者是出现在组队界面， 那就是可能房间死了
                # 要结束任务
                sleep(0.5)
                if self.appear(self.I_MATCHING) or self.appear(self.I_CHECK_EXPLORATION):
                    sleep(0.5)
                    if self.appear(self.I_MATCHING) or self.appear(self.I_CHECK_EXPLORATION):
                        logger.warning('EvoZone task failed')
                        success = False
                        break
                continue

            # 点击挑战
            if not is_first:
                if self.run_invite(config=self.config.evo_zone.invite_config):
                    self.run_general_battle(config=self.config.evo_zone.general_battle_config)
                else:
                    # 邀请失败，退出任务
                    logger.warning('Invite failed and exit this EvoZone task')
                    success = False
                    break

            # 第一次会邀请队友
            if is_first:
                if not self.run_invite(config=self.config.evo_zone.invite_config, is_first=True):
                    logger.warning('Invite failed and exit this evozone task')
                    success = False
                    break
                else:
                    is_first = False
                    self.run_general_battle(config=self.config.evo_zone.general_battle_config)

        # 当结束或者是失败退出循环的时候只有两个UI的可能，在房间或者是在组队界面
        # 如果在房间就退出
        if self.exit_room():
            pass
        # 如果在组队界面就退出
        if self.exit_team():
            pass

        self.goto_page(page_main)

        if not success:
            return False
        return True

    def run_member(self):
        logger.info('Start run member')
        # self.ui_goto(page_soul_zones)
        # self.evozone_enter()
        # self.check_lock(self.config.evo_zone.general_battle_config.lock_team_enable)

        # 进入战斗流程
        self.device.stuck_record_add('BATTLE_STATUS_S')
        while 1:
            self.screenshot()

            # 检查猫咪奖励
            if self.appear_then_click(self.I_PET_PRESENT, action=self.C_WIN_3, interval=1):
                continue

            if self.current_count >= self.limit_count:
                logger.info('EvoZone count limit out')
                break
            if datetime.now() - self.start_time >= self.limit_time:
                logger.info('EvoZone time limit out')
                break

            if self.check_then_accept():
                continue

            if self.is_in_room():
                self.device.stuck_record_clear()
                if self.wait_battle(wait_time=self.config.evo_zone.invite_config.wait_time):
                    self.run_general_battle(config=self.config.evo_zone.general_battle_config)
                else:
                    break
            # 队长秒开的时候，检测是否进入到战斗中
            elif self.check_take_over_battle(False, config=self.config.evo_zone.general_battle_config):
                continue

        while 1:
            # 有一种情况是本来要退出的，但是队长邀请了进入的战斗的加载界面
            if self.appear(self.I_GI_HOME) or self.appear(self.I_GI_EXPLORE):
                break
            # 如果可能在房间就退出
            if self.exit_room():
                pass
            # 如果还在战斗中，就退出战斗
            if self.exit_battle():
                pass

        self.goto_page(page_main)
        return True

    def run_alone(self):
        logger.info('Start run alone')
        self.goto_page(page_awake_zones)
        self.evozone_enter()
        layer = self.config.evo_zone.evo_zone_config.layer
        self.check_layer(layer)
        self.check_lock(self.config.evo_zone.general_battle_config.lock_team_enable)

        def is_in_evozone(screenshot=False) -> bool:
            if screenshot:
                self.screenshot()
            return self.appear(self.I_EVOZONE_FIRE)

        while 1:
            self.screenshot()

            # 检查猫咪奖励
            if self.appear_then_click(self.I_PET_PRESENT, action=self.C_WIN_3, interval=1):
                continue

            if not is_in_evozone():
                continue

            if self.current_count >= self.limit_count:
                logger.info('EvoZone count limit out')
                break
            if datetime.now() - self.start_time >= self.limit_time:
                logger.info('EvoZone time limit out')
                break

            # 点击挑战
            while 1:
                self.screenshot()
                if self.appear_then_click(self.I_EVOZONE_FIRE, interval=1):
                    pass

                if not self.appear(self.I_EVOZONE_FIRE):
                    self.run_general_battle(config=self.config.evo_zone.general_battle_config)
                    break

        # 回去
        while 1:
            self.screenshot()
            if not self.appear(self.I_FORM_TEAM):
                break
            if self.appear_then_click(self.I_BACK_Y, interval=1):
                continue

        self.goto_page(page_main)

    def run_wild(self):
        logger.error('Wild mode is not implemented')
        pass


    @battle_wait_strategy()
    def battle_wait(self, *args, **kwargs):
        return self.battle_wait_with_strategy(*args, **kwargs)



if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)

    t.run()

    # t.check_layer('悲')

    from module.base.timer import timer
