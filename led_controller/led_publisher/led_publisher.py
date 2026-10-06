"""示例：从自己的节点发布命令到 led_controller 的现有话题。

话题与数据格式（std_msgs/Int32MultiArray，索引从 0 开始、闭区间）:
  /ares/led/segment    [总数, left, right, R, G, B]
  /ares/led/single     [总数, left, right, R,G,B, R,G,B, ...]  (长度 3+3*灯数)
  /ares/led/animation  [总数, left, right, 效果ID, 可选参数...]

运行前需先启动 led_node（或本包用 backend:=dummy 演练）:
  ros2 run led_controller led_node --ros-args -p backend:=dummy
  ros2 run led_controller led_pub --ros-args -p led_count:=144
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32MultiArray


class LedPublisher(Node):

    def __init__(self):
        super().__init__('led_publisher')
        self.declare_parameter('led_count', 144)
        self.count = int(self.get_parameter('led_count').value)

        self.segment_pub = self.create_publisher(
            Int32MultiArray, '/ares/led/segment', 10)
        self.single_pub = self.create_publisher(
            Int32MultiArray, '/ares/led/single', 10)
        self.animation_pub = self.create_publisher(
            Int32MultiArray, '/ares/led/animation', 10)

        self.step = 0
        self.create_timer(3.0, self.tick)

    def publish_segment(self, left, right, r, g, b):
        """用同一种颜色点亮闭区间 [left, right]。"""
        msg = Int32MultiArray()
        msg.data = [self.count, left, right, r, g, b]
        self.segment_pub.publish(msg)

    def publish_pixels(self, left, colors):
        """逐颗设置，colors 是 [(r,g,b), ...]，从 left 开始。"""
        right = left + len(colors) - 1
        data = [self.count, left, right]
        for r, g, b in colors:
            data.extend((r, g, b))
        msg = Int32MultiArray()
        msg.data = data
        self.single_pub.publish(msg)

    def publish_animation(self, left, right, effect, params=()):
        """effect 见 led_controller：0 停止, 3 彩虹, 10 彗星 ..."""
        msg = Int32MultiArray()
        msg.data = [self.count, left, right, effect, *params]
        self.animation_pub.publish(msg)

    def tick(self):
        if self.step == 0:
            self.publish_segment(0, self.count - 1, 255, 0, 0)
            self.get_logger().info('全红')
        elif self.step == 1:
            self.publish_segment(0, self.count - 1, 0, 0, 0)          # 先全灭
            left = self.count // 4
            right = self.count * 3 // 4 - 1
            self.publish_segment(left, right, 225, 0, 0)              # 中间红色
            self.get_logger().info('中间红色')
        elif self.step == 2:
            self.publish_animation(0, self.count - 1, 3)              # 彩虹
            self.get_logger().info('彩虹动画')
        else:
            self.publish_animation(0, self.count - 1, 0)              # 停止
            self.get_logger().info('停止')
        self.step = (self.step + 1) % 4


def main(args=None):
    rclpy.init(args=args)
    node = LedPublisher()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
