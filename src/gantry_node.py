#!/usr/bin/env python2

NODE_NAME = "GantryNode"

import rospy
import numpy as np
import ctypes

from threading import Thread

from can_msgs.msg import Frame
from geometry_msgs.msg import Pose, Twist, TransformStamped
from nav_msgs.msg import Odometry

import tf2_ros
# import tf_conversions

from Controls import PIDController, LPController
from Utils import Vector

class numhex64(ctypes.Union):
    _fields_ = [("num", ctypes.c_double),
                ("sint", ctypes.c_int64),
                ("uint", ctypes.c_uint64),
                ("hex", ctypes.c_ubyte * 8)]

class numhex32(ctypes.Union):
    _fields_ = [("num", ctypes.c_float),
                ("sint", ctypes.c_int32),
                ("uint", ctypes.c_uint32),
                ("hex", ctypes.c_ubyte * 4)]

class GantryNode:
    """
    This node interfaces with the ODrive boards and attached encoders, to drive the gantry at either a desired velocity or to a desired position

    Reports gantry position and pendant status at some frequency

    positive x: towards door
        positive y: towards machine wall

        y   [machine wall]
        ^  ______
        | |gantry|  [door]
        | |______|
        |-----> x

    """
    COUNTS_PER_M = 5530. # found experimentally
    M_PER_COUNT = 1./COUNTS_PER_M
    RADS_PER_M = 37. # found experimentally


    OFFSET = [0,0] # in meters

    # format:        x bounds, y bounds
    POSITION_BOUNDS = ((0,2), (-1,1))

    DATA_LENGTH = {
        'double':8,
        'int32':4,
        'bool':1,
        'bool3':3
    }


    # Experimental data:
    #     id (hex)  data_length
    # ['1'   '0x1'   '8'] < double - control
    # ['2'   '0x2'   '8'] < double - control
    # [''    '0x4'   '4']
    # ['16'  '0x10'  '3'] < bool3 - status
    # ['17'  '0x11'  '8'] < double
    # ['18'  '0x12'  '8'] < double
    # ['19'  '0x13'  '8'] < double
    # ['32'  '0x20'  '1'] < bool1 - status
    # ['33'  '0x21'  '8'] < double
    # ['257' '0x101' '4'] < int32
    # ['258' '0x102' '4'] < int32
    # ['259' '0x103' '4'] < int32
    # ['260' '0x104' '4'] < int32
    # ['261' '0x105' '4'] < int32
    # ['273' '0x111' '4'] < int32
    # ['274' '0x112' '4'] < int32
    # ['275' '0x113' '4'] < int32
    # ['276' '0x114' '4'] < int32
    # ['277' '0x115' '4'] < int32
    # ['290' '0x122' '4'] < int32
    # ['291' '0x123' '4'] < int32
    # ['292' '0x124' '4'] < int32
    # ['293' '0x125' '4'] < int32
    # message types: 22

    NAME_TABLE = {
        # rad/sec
        'control_x_speed': [0x01, 'double'], # YES
        'control_y_speed': [0x02, 'double'], # YES
        
        'main_crane_driver_status': [0x10, 'bool3'], # LIKELY
        'sub_crane_driver_status': [0x20, 'double'], # LIKELY

        'x_axis_actual_control_speed_0': [0x11, 'double'],  # LIKELY
        'x_axis_actual_control_speed_1': [0x12, 'double'],  # LIKELY
        'y_axis_actual_control_speed': [0x13, 'double'],    # LIKELY
        
        'y_axis_actual_control_speed_mirror': [0x21, 'double'], # LIKELY

        # mm
        'laser_distance_0': [0x101, 'int32'],
        'laser_distance_1': [0x111, 'int32'],

        # Counts, counts/sec (2048 counts = 1rev = 300mm)
        'encoder_ppr_0': [0x102, 'int32'],
        'encoder_inc_0': [0x103, 'int32'],
        'encoder_abs_0': [0x104, 'int32'],
        'encoder_speed_0': [0x105, 'int32'],
        
        'encoder_ppr_1': [0x112, 'int32'],
        'encoder_inc_1': [0x113, 'int32'],
        'encoder_abs_1': [0x114, 'int32'],
        'encoder_speed_1': [0x115, 'int32'],

        'encoder_ppr_2': [0x122, 'int32'],
        'encoder_inc_2': [0x123, 'int32'],
        'encoder_abs_2': [0x124, 'int32'],
        'encoder_speed_2': [0x125, 'int32'],


        # 'reset_encoder_0': [0x100, 'bool'],
        # 'reset_encoder_1': [0x200, 'bool'],
        # 'reset_encoder_2': [0x300, 'bool'],
    }
    ID_TABLE = {}
    CAN_STATE = {}

    
    RATE = 20 # hz

    def __init__(self):
        self.init_state()
        
        # Publishers
        self.init_publishers()

        # Subscribers
        self.init_subscribers()


        rospy.logwarn(NODE_NAME + " is initializing...")
        # Await lidar distance estimation
        while self.CAN_STATE['laser_distance_0'] == None or self.CAN_STATE['laser_distance_1'] == None:
            rospy.sleep(0.1)
        
        # Set approximate map -> base_link transform
        
        # Later: use limit switches to find precise y coordinate
        # Much Later: use limit switches to find precise x coordinate
        # go to home position
        

        rospy.logwarn(NODE_NAME + " is online")

        rate = rospy.Rate(self.RATE)
        self.prev_time = rospy.get_rostime().to_sec() - 1.0/self.RATE
        while not rospy.is_shutdown():
            self.loop_callback()
            rate.sleep()

    def init_publishers(self):
        self.tf_broadcaster = tf2_ros.TransformBroadcaster()

        self.state_pub = rospy.Publisher(
            rospy.get_param("~/gantry_state_topic", "gantry/state"),
            JointState, queue_size=1)

            # received by the CAN node on the gantry (which in turn speaks to the CAN arduino over serial via dark and unknowable magics)
        self.gantry_pub = rospy.Publisher(
            rospy.get_param('~/can_sent_messages','/sent_messages'),
            Frame, queue_size=1)

    def init_subscribers(self):
        self.tf_buffer = tf2_ros.Buffer()
        tf2_ros.TransformListener(self.tf_buffer)

        rospy.Subscriber(
            rospy.get_param("~/can_received_messages", "/received_messages"),
            Frame, self.can_message_callback, queue_size=10)

        rospy.Subscriber(
            rospy.get_param("~/gantry_control_topic", "gantry/control"),
            JointState, self.control_callback, queue_size=1)
    
    def init_state(self):
        # copy NAME_TABLE into ID_TABLE, initialize CAN_STATE
        for key in self.NAME_TABLE.keys():
            id, can_type = self.NAME_TABLE[key]
            id = hex(id)
            self.ID_TABLE[id] = [key, can_type]
            self.CAN_STATE[key] = None
        
        
        self.velocity_controller = LPController(0.1)
        self.position_controller = PIDController(1.,0.001,0.001)

        self.target_velocity = [0,0]
        self.target_position = None

        self.velocity = [0,0]
        self.position = [0,0] # TODO: Get this from encoder data
        
    def loop_callback(self):
        """
        Write velocity, do position control, and publish any updates
        """
        time = rospy.get_rostime().to_sec()
        delta_t = time - self.prev_time
        self.prev_time = time
        
        if not self.target_velocity is None:
            velocity_control = self.velocity_controller.do_control(self.velocity, self.target_velocity, delta_t)
        else:
            velocity_control = self.position_controller.do_control(self.position, self.target_position, delta_t)

        self.write_velocity(velocity_control)
        # self.write_velocity(self.target_velocity)

        x_speed = np.mean([ self.CAN_STATE['encoder_speed_0'],
                            self.CAN_STATE['encoder_speed_1'] ])  * self.M_PER_COUNT
        x_pos = np.mean([ self.CAN_STATE['encoder_abs_0'],
                            self.CAN_STATE['encoder_abs_1'] ]) * self.M_PER_COUNT + self.OFFSET[1]
        y_speed = self.CAN_STATE['encoder_speed_2'] * self.M_PER_COUNT
        y_pos = self.CAN_STATE['encoder_abs_2'] * self.M_PER_COUNT + self.OFFSET[1]


        # self.velocity = velocity_control # TODO: get this from sensor feedback
        self.velocity = [x_speed, y_speed]
        self.position = [x_pos, y_pos]

        self.publish_state()

        rospy.loginfo( x_speed )



    def control_callback(self, msg):
        """
        Set the target velocity of the gantry
        """
        
        self.target_position = Vector.to_array(msg.position)
        self.target_velocity = Vector.to_array(msg.velocity)

        if not self.target_position is None:
            self.target_position = self.target_position[:2]
            rospy.loginfo("Position set to x: %f rps, y: %f rps", self.target_position)
        else:
            self.target_velocity = self.target_velocity[:2]
            rospy.loginfo("Velocity set to x: %f rps, y: %f rps", *self.target_velocity)
        
    def can_message_callback(self, msg):
        """
        Process the messages recieved from the CAN node. Store them in state
        """
        
        if not hex(msg.id) in self.ID_TABLE.keys():
            rospy.logwarn("unknown id: %s with length %s" % (hex(msg.id), msg.dlc))
            return
        
        key, can_type = self.ID_TABLE[hex(msg.id)]


        if can_type == 'double':
            numhex = numhex64()
        else:
            numhex = numhex32()
        
        for idx in range(msg.dlc):
            numhex.hex[idx] = ord(msg.data[idx])

        if can_type == 'int32':
            val = numhex.sint
        elif can_type == 'double':
            val = numhex.num
        else:
            val = numhex.sint != 0
        
        self.CAN_STATE[key] = val
        return (key, numhex.sint, numhex.num)

    def send_can_frame(self, name, value, publish=True):
        """
        A reasonably generic way to send message to the CAN node that doesnt rot the mind
        """

        id, can_type = self.NAME_TABLE[name]
        length = self.DATA_LENGTH[can_type]
        
        msg = Frame()
        msg.is_rtr = False
        msg.is_extended = False
        msg.dlc = length
        msg.id = id
        msg.data = ""
        
        if can_type == 'int32':
            numhex = numhex32()
            numhex.sint = value
        elif can_type == 'double':
            numhex = numhex64()
            numhex.num = value
        else:
            rospy.logwarn("Unknown type to send: " + can_type)
            return
        # elif can_type == 'bool':
        #     numhex = numhex64()
        #     for idx in range(8):
        #         numhex.hex[idx] = 0xff
        
        for idx in range(length):
            msg.data += chr(numhex.hex[idx])
        
        msg.header.stamp = rospy.get_rostime()
        if publish: self.gantry_pub.publish(msg)

        return msg

    def write_velocity(self, velocity):
        """
        Send velocity commands to the gantry motors
        """
        velocity = np.array(velocity) * self.RADS_PER_M

        self.send_can_frame('control_x_speed', velocity[0])
        self.send_can_frame('control_y_speed', velocity[1])

    def publish_state(self):
        """
        Report the position and velocity of the gantry, computed from encoder counts
        """
        coord = Coord(self.position, [0,0,0,1])
        
        msg = TransformStamped()
        msg.header.stamp = rospy.get_rostime()
        msg.header.frame_id = 'map'
        msg.child_frame_id = 'base_link'
        msg.transform = coord.to_tf()

        self.tf_broadcaster.sendTransform(msg)

        msg = JointState()
        msg.position = Vector(self.position)
        msg.velocity = Vector(self.velocity)

        self.state_pub.publish(msg)

if __name__ == '__main__':
    # init ros node
    rospy.init_node(NODE_NAME, anonymous=True)
    g = GantryNode()

    rospy.spin()