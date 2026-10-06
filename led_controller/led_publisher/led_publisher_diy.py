"""可定制的 LED 发布者：支持「参数模式」和「交互菜单模式」。

依赖 led_controller 的现有话题（std_msgs/Int32MultiArray，索引 0 起、闭区间）:
  /ares/led/segment    [总数, left, right, R, G, B]
  /ares/led/single     [总数, left, right, R,G,B, ...]
  /ares/led/animation  [总数, left, right, 效果ID, 参数...]

先启动控制节点（实机，或用 dummy 演练）:
  ros2 run led_controller led_node
  ros2 run led_controller led_node --ros-args -p backend:=dummy

【交互菜单模式】运行时列出选项, 让你挑选:
  ros2 run led_controller led_pub_diy --ros-args -p interactive:=true

【参数模式】直接用参数指定, 无需交互:
  ros2 run led_controller led_pub_diy --ros-args \
    -p led_count:=144 -p mode:=segment -p left:=0 -p right:=29 \
    -p red:=255 -p green:=0 -p blue:=0 -p brightness:=0.5

参数:
  interactive      true 时进入交互菜单模式 (默认 false)
  led_count        灯珠总数
  mode             segment | single | animation
  left / right     闭区间
  red/green/blue   0-255
  brightness       0.0-1.0, 软件缩放 RGB
  effect           animation 效果ID
  params           animation 参数, 逗号字符串 "0,255,255,12,1"
  colors           single 逐颗颜色, 逗号字符串 "255,0,0,0,0,255"
  period           发布周期(秒); <=0 只发一次
  burst            一次性模式重复次数
  off_on_exit      退出时自动熄灭 (默认 true)
  *_topic          三个话题名
"""

from __future__ import annotations

import signal

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.parameter import Parameter
from std_msgs.msg import Int32MultiArray


def _parse_ints(text) -> list:
    """把 "1, 2, 3" 解析成 [1, 2, 3]；空串返回 []。"""
    if text is None:
        return []
    if isinstance(text, (list, tuple)):
        return [int(v) for v in text]
    result = []
    for part in str(text).split(','):
        part = part.strip()
        if part:
            result.append(int(part))
    return result


def _clamp_byte(value) -> int:
    return max(0, min(255, int(round(value))))


def _scale(rgb, brightness: float) -> tuple:
    return tuple(_clamp_byte(channel * brightness) for channel in rgb)


EFFECTS = {
    0: ('停止并保持', '无参数'),
    1: ('正向追逐', 'R,G,B,width'),
    2: ('反向追逐', 'R,G,B,width'),
    3: ('彩虹循环', 'speed(1~255)'),
    4: ('剧院灯追逐', 'R,G,B,gap'),
    5: ('呼吸灯', 'R,G,B,周期帧(>=2)'),
    6: ('往返扫描', 'R,G,B,width'),
    7: ('随机闪烁', 'R,G,B,density(1~100)'),
    8: ('颜色擦除', 'R,G,B,direction(±1)'),
    9: ('双色跑马', 'R1,G1,B1,R2,G2,B2,block'),
    10: ('彗星拖尾', 'R,G,B,tail,direction(±1)'),
}


class LedPublisherDiy(Node):

    def __init__(self) -> None:
        super().__init__('led_publisher_diy')

        self.declare_parameter('interactive', False)
        self.declare_parameter('led_count', 144)
        self.declare_parameter('mode', 'segment')
        self.declare_parameter('left', 0)
        self.declare_parameter('right', 29)
        self.declare_parameter('red', 255)
        self.declare_parameter('green', 0)
        self.declare_parameter('blue', 0)
        self.declare_parameter('brightness', 1.0)
        self.declare_parameter('effect', 1)
        self.declare_parameter('params', '')
        self.declare_parameter('colors', '')
        self.declare_parameter('period', 1.0)
        self.declare_parameter('burst', 3)
        self.declare_parameter('off_on_exit', True)
        self.declare_parameter('segment_topic', '/ares/led/segment')
        self.declare_parameter('single_topic', '/ares/led/single')
        self.declare_parameter('animation_topic', '/ares/led/animation')

        self.segment_pub = self.create_publisher(
            Int32MultiArray, self.get_parameter('segment_topic').value, 10)
        self.single_pub = self.create_publisher(
            Int32MultiArray, self.get_parameter('single_topic').value, 10)
        self.animation_pub = self.create_publisher(
            Int32MultiArray, self.get_parameter('animation_topic').value, 10)

        self.interactive = bool(self.get_parameter('interactive').value)
        self._sent = 0
        self._last_time = None
        if not self.interactive:
            self.create_timer(0.05, self._tick)

        self.get_logger().info(
            'led_publisher_diy 就绪; 交互模式用 -p interactive:=true')

    # ---------- 通用发送 ----------

    def _count(self) -> int:
        return int(self.get_parameter('led_count').value)

    def _send(self, publisher, data, label: str, repeat: int = 1) -> None:
        msg = Int32MultiArray()
        msg.data = [int(v) for v in data]
        for _ in range(repeat):
            publisher.publish(msg)
            rclpy.spin_once(self, timeout_sec=0.02)
        self.get_logger().info(f'{label} data={msg.data}')

    # ---------- 参数模式 ----------

    def _publish_once(self) -> None:
        count = self._count()
        if count < 1:
            self.get_logger().error('led_count 必须 >= 1')
            return
        left = int(self.get_parameter('left').value)
        right = int(self.get_parameter('right').value)
        brightness = float(self.get_parameter('brightness').value)
        mode = str(self.get_parameter('mode').value).lower()
        base = _scale(
            (self.get_parameter('red').value,
             self.get_parameter('green').value,
             self.get_parameter('blue').value),
            brightness,
        )

        if mode == 'segment':
            self._send(self.segment_pub, [count, left, right, *base], 'segment')
        elif mode == 'single':
            colors = _parse_ints(self.get_parameter('colors').value)
            if len(colors) >= 3:
                scaled = []
                for index in range(0, len(colors) - 2, 3):
                    scaled.extend(_scale(colors[index:index + 3], brightness))
                effective_right = left + len(scaled) // 3 - 1
                data = [count, left, effective_right, *scaled]
            else:
                data = [count, left, right]
                for _ in range(right - left + 1):
                    data.extend(base)
            self._send(self.single_pub, data, 'single')
        elif mode == 'animation':
            effect = int(self.get_parameter('effect').value)
            params = _parse_ints(self.get_parameter('params').value)
            self._send(self.animation_pub, [count, left, right, effect, *params],
                       'animation')
        else:
            self.get_logger().error(
                f'未知 mode: {mode} (可选 segment / single / animation)')

    def _tick(self) -> None:
        try:
            period = float(self.get_parameter('period').value)
            burst = max(1, int(self.get_parameter('burst').value))
        except (TypeError, ValueError) as exc:
            self.get_logger().error(f'参数类型错误: {exc}')
            return

        if period <= 0.0:
            if self._sent < burst:
                self._publish_once()
                self._sent += 1
                if self._sent >= burst:
                    self.get_logger().info(f'一次性发送完成 (共 {burst} 次)')
            return

        now = self.get_clock().now()
        if self._last_time is not None:
            elapsed = (now - self._last_time).nanoseconds / 1e9
            if elapsed < period:
                return
        self._last_time = now
        self._publish_once()
        self._sent += 1

    # ---------- 交互菜单模式 ----------

    def _ask(self, label, default, cast, minimum=None, maximum=None):
        """逐项询问参数: 显示取值范围, 越界或非法输入会重新询问。"""
        kind = '整数' if cast is int else '数字'
        suffix = f' [{default}]' if default is not None else ''
        while True:
            raw = input(f'  {label}{suffix}: ').strip()
            if raw == '' and default is not None:
                value = default
            else:
                try:
                    value = cast(raw)
                except ValueError:
                    print(f'    × 请输入有效的{kind}')
                    continue
            if minimum is not None and value < minimum:
                print(f'    × 超出范围: 最小为 {minimum}')
                continue
            if maximum is not None and value > maximum:
                print(f'    × 超出范围: 最大为 {maximum}')
                continue
            return value

    def _menu(self) -> str:
        count = self._count()
        print('\n' + '=' * 46)
        print(f' LED 定制发布器  (灯珠总数: {count})')
        print('=' * 46)
        print('  1) 一段单色        (segment)')
        print('  2) 逐颗颜色        (single)')
        print('  3) 播放动画        (animation)')
        print('  4) 整条熄灭')
        print('  5) 修改灯珠总数')
        print('  0) 退出 (自动熄灭)')
        return input('请选择> ').strip()

    def run_interactive(self) -> None:
        print('\n交互模式启动。请确保 led_node 已在运行。')
        while True:
            choice = self._menu()
            try:
                if choice == '1':
                    self._menu_segment()
                elif choice == '2':
                    self._menu_single()
                elif choice == '3':
                    self._menu_animation()
                elif choice == '4':
                    count = self._count()
                    self._send(self.segment_pub, [count, 0, count - 1, 0, 0, 0],
                               '熄灭', repeat=3)
                elif choice == '5':
                    new_count = self._ask(
                        '新的灯珠总数 (1~1000)', self._count(), int, 1, 1000)
                    self.set_parameters(
                        [Parameter('led_count', value=int(new_count))])
                    print(f'  ✔ 灯珠总数已改为 {new_count}')
                elif choice == '0':
                    break
                else:
                    print('  无效选择')
            except (EOFError, KeyboardInterrupt):
                print()
                break
            except ValueError as exc:
                print(f'  输入有误: {exc}')

    def _menu_segment(self) -> None:
        count = self._count()
        print(f'\n-- 一段单色 segment  (灯珠索引范围 0 ~ {count - 1}) --')
        left = self._ask(
            f'[1/6] 起始灯珠索引 left (0~{count - 1})', 0, int, 0, count - 1)
        right = self._ask(
            f'[2/6] 结束灯珠索引 right ({left}~{count - 1})',
            count - 1, int, left, count - 1)
        r = self._ask('[3/6] 红色 R (0~255)', 255, int, 0, 255)
        g = self._ask('[4/6] 绿色 G (0~255)', 0, int, 0, 255)
        b = self._ask('[5/6] 蓝色 B (0~255)', 0, int, 0, 255)
        brightness = self._ask(
            '[6/6] 亮度 brightness (0.0~1.0)', 1.0, float, 0.0, 1.0)
        rgb = _scale((r, g, b), brightness)
        self._send(self.segment_pub, [count, left, right, *rgb],
                   f'segment [{left},{right}]', repeat=3)

    def _menu_single(self) -> None:
        count = self._count()
        print(f'\n-- 逐颗颜色 single  (灯珠索引范围 0 ~ {count - 1}) --')
        left = self._ask(
            f'[1/2] 起始灯珠索引 left (0~{count - 1})', 0, int, 0, count - 1)
        print('  [2/2] 选择输入方式:')
        print('        · 逐颗设置: 输入 "r,g,b, r,g,b, ..."')
        print('          (每颗 3 个数 = R,G,B, 每个 0~255, 用逗号分隔)')
        print('        · 单色填充: 直接回车')
        raw = input('  逐颗颜色 (回车=单色填充): ').strip()
        if raw:
            try:
                colors = _parse_ints(raw)
            except ValueError:
                print('    × 必须是逗号分隔的整数')
                return
            if len(colors) < 3 or len(colors) % 3:
                print('    × 颜色个数必须是 3 的倍数 (每颗 R,G,B)')
                return
            if any(c < 0 or c > 255 for c in colors):
                print('    × 每个颜色通道必须在 0~255')
                return
            right = left + len(colors) // 3 - 1
            if right > count - 1:
                print(f'    × 超出灯带范围 (right={right} > {count - 1})')
                return
            self._send(self.single_pub, [count, left, right, *colors],
                       f'single [{left},{right}]', repeat=3)
        else:
            print('  [续] 单色填充参数:')
            right = self._ask(
                f'        结束灯珠索引 right ({left}~{count - 1})',
                count - 1, int, left, count - 1)
            r = self._ask('        红色 R (0~255)', 255, int, 0, 255)
            g = self._ask('        绿色 G (0~255)', 0, int, 0, 255)
            b = self._ask('        蓝色 B (0~255)', 0, int, 0, 255)
            data = [count, left, right]
            for _ in range(right - left + 1):
                data.extend((r, g, b))
            self._send(self.single_pub, data,
                       f'single [{left},{right}] 单色填充', repeat=3)

    def _menu_animation(self) -> None:
        print('\n-- 播放动画 animation --')
        print('  可用效果 (ID: 名称 / 参数说明):')
        for eid, (name, params) in EFFECTS.items():
            print(f'    {eid:>2}) {name:<10} {params}')
        count = self._count()
        effect = self._ask(
            f'[1/4] 效果ID (0~{max(EFFECTS)}, 见上表)', 3, int, 0, max(EFFECTS))
        left = self._ask(
            f'[2/4] 起始灯珠索引 left (0~{count - 1})', 0, int, 0, count - 1)
        right = self._ask(
            f'[3/4] 结束灯珠索引 right ({left}~{count - 1})',
            count - 1, int, left, count - 1)
        print('  [4/4] 参数 (逗号分隔整数, 参照上表该效果的参数说明)')
        print('        直接回车 = 使用该效果的默认参数')
        raw = input('  参数: ').strip()
        try:
            params = _parse_ints(raw)
        except ValueError:
            print('    × 参数必须是逗号分隔的整数')
            return
        self._send(self.animation_pub, [count, left, right, effect, *params],
                   f'animation effect={effect}', repeat=3)

    # ---------- 退出 ----------

    def turn_off(self) -> None:
        count = self._count()
        if count < 1:
            return
        self._send(self.segment_pub, [count, 0, count - 1, 0, 0, 0],
                   '退出熄灭', repeat=3)


def _raise_keyboard_interrupt(signum, frame):
    # 覆盖 rclpy 的 SIGINT 处理, 让它只抛异常而不关上下文,
    # 这样 finally 里还能把"熄灭"命令发出去。
    raise KeyboardInterrupt


def main(args=None) -> None:
    rclpy.init(args=args)
    node = LedPublisherDiy()

    signal.signal(signal.SIGINT, _raise_keyboard_interrupt)
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    try:
        if node.interactive:
            node.run_interactive()
        else:
            rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node.get_parameter('off_on_exit').value:
            try:
                node.turn_off()
            except Exception as exc:  # noqa: BLE001
                node.get_logger().warn(f'退出熄灭失败: {exc}')
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
