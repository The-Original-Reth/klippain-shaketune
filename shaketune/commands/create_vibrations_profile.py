# Shake&Tune: 3D printer analysis tools
#
# Copyright (C) 2024 Félix Boisselier <felix@fboisselier.fr> (Frix_x on Discord)
# Licensed under the GNU General Public License v3.0 (GPL-3.0)
#
# File: vibrations_profile.py
# Description: Provides a command to measure the vibrations generated by the kinematics and motors of a 3D printers
#              at different speeds and angles increments. The data is collected from the accelerometer and used
#              to generate a comprehensive vibration analysis graph.


import math

from ..helpers.accelerometer import Accelerometer, MeasurementsManager
from ..helpers.console_output import ConsoleOutput
from ..helpers.motors_config_parser import MotorsConfigParser
from ..shaketune_process import ShakeTuneProcess

MIN_SPEED = 2  # mm/s


def create_vibrations_profile(gcmd, config, st_process: ShakeTuneProcess) -> None:
    size = gcmd.get_float('SIZE', default=100.0, minval=50.0)
    z_height = gcmd.get_float('Z_HEIGHT', default=20.0)
    max_speed = gcmd.get_float('MAX_SPEED', default=200.0, minval=10.0)
    speed_increment = gcmd.get_float('SPEED_INCREMENT', default=2.0, minval=1.0)
    accel = gcmd.get_int('ACCEL', default=3000, minval=100)
    feedrate_travel = gcmd.get_float('TRAVEL_SPEED', default=120.0, minval=20.0)
    accel_chip = gcmd.get('ACCEL_CHIP', default=None)

    if accel_chip == '':
        accel_chip = None

    if (size / (max_speed / 60)) < 0.25:
        raise gcmd.error(
            'The size of the movement is too small for the given speed! Increase SIZE or decrease MAX_SPEED!'
        )

    printer = config.get_printer()
    gcode = printer.lookup_object('gcode')
    toolhead = printer.lookup_object('toolhead')
    input_shaper = printer.lookup_object('input_shaper', None)
    systime = printer.get_reactor().monotonic()

    # Check that input shaper is already configured
    if input_shaper is None:
        raise gcmd.error('Input shaper is not configured! Please run the shaper calibration macro first.')

    motors_config_parser = MotorsConfigParser(config, motors=['stepper_x', 'stepper_y'])
    if motors_config_parser.kinematics in {'cartesian', 'corexz'}:
        main_angles = [0, 90]  # Cartesian motors are on X and Y axis directly, same for CoreXZ
    elif motors_config_parser.kinematics == 'corexy':
        main_angles = [45, 135]  # CoreXY motors are on A and B axis (45 and 135 degrees)
    else:
        raise gcmd.error(
            'Only Cartesian, CoreXY and CoreXZ kinematics are supported at the moment for the vibrations measurement tool!'
        )
    ConsoleOutput.print(f'{motors_config_parser.kinematics.upper()} kinematics mode')

    toolhead_info = toolhead.get_status(systime)
    old_accel = toolhead_info['max_accel']
    old_sqv = toolhead_info['square_corner_velocity']

    # set the wanted acceleration values
    if 'minimum_cruise_ratio' in toolhead_info:  # minimum_cruise_ratio found: Klipper >= v0.12.0-239
        old_mcr = toolhead_info['minimum_cruise_ratio']
        gcode.run_script_from_command(
            f'SET_VELOCITY_LIMIT ACCEL={accel} MINIMUM_CRUISE_RATIO=0 SQUARE_CORNER_VELOCITY=5.0'
        )
    else:  # minimum_cruise_ratio not found: Klipper < v0.12.0-239
        old_mcr = None
        gcode.run_script_from_command(f'SET_VELOCITY_LIMIT ACCEL={accel} SQUARE_CORNER_VELOCITY=5.0')

    kin_info = toolhead.kin.get_status(systime)
    mid_x = (kin_info['axis_minimum'].x + kin_info['axis_maximum'].x) / 2
    mid_y = (kin_info['axis_minimum'].y + kin_info['axis_maximum'].y) / 2
    X, Y, _, E = toolhead.get_position()

    # Going to the start position
    toolhead.move([X, Y, z_height, E], feedrate_travel / 10)
    toolhead.move([mid_x - 15, mid_y - 15, z_height, E], feedrate_travel)
    toolhead.dwell(0.5)

    measurements_manager = MeasurementsManager(printer.get_reactor())

    nb_speed_samples = int((max_speed - MIN_SPEED) / speed_increment + 1)
    for curr_angle in main_angles:
        ConsoleOutput.print(f'-> Measuring angle: {curr_angle} degrees...')
        radian_angle = math.radians(curr_angle)

        # Map angles to accelerometer axes and default to 'xy' if angle is not 0 or 90 degrees
        # and then find the best accelerometer chip for the current angle if not manually specified
        angle_to_axis = {0: 'x', 90: 'y'}
        accel_axis = angle_to_axis.get(curr_angle, 'xy')
        current_accel_chip = accel_chip  # to retain the manually specified chip
        if current_accel_chip is None:
            current_accel_chip = Accelerometer.find_axis_accelerometer(printer, accel_axis)
        k_accelerometer = printer.lookup_object(current_accel_chip, None)
        if k_accelerometer is None:
            raise gcmd.error(f'Accelerometer [{current_accel_chip}] not found!')
        ConsoleOutput.print(f'Accelerometer chip used for this angle: [{current_accel_chip}]')
        accelerometer = Accelerometer(k_accelerometer, printer.get_reactor())

        # Sweep the speed range to record the vibrations at different speeds
        for curr_speed_sample in range(nb_speed_samples):
            curr_speed = MIN_SPEED + curr_speed_sample * speed_increment
            ConsoleOutput.print(f'Current speed: {curr_speed} mm/s')

            # Reduce the segments length for the lower speed range (0-100mm/s). The minimum length is 1/3 of the SIZE and is gradually increased
            # to the nominal SIZE at 100mm/s. No further size changes are made above this speed. The goal is to ensure that the print head moves
            # enough to collect enough data for vibration analysis, without doing unnecessary distance to save time. At higher speeds, the full
            # segments lengths are used because the head moves faster and travels more distance in the same amount of time and we want enough data
            if curr_speed < 100:
                segment_length_multiplier = 1 / 5 + 4 / 5 * curr_speed / 100
            else:
                segment_length_multiplier = 1

            # Calculate angle coordinates using trigonometry and length multiplier and move to start point
            dX = (size / 2) * math.cos(radian_angle) * segment_length_multiplier
            dY = (size / 2) * math.sin(radian_angle) * segment_length_multiplier
            toolhead.move([mid_x - dX, mid_y - dY, z_height, E], feedrate_travel)

            # Adjust the number of back and forth movements based on speed to also save time on lower speed range
            # 3 movements are done by default, reduced to 2 between 150-250mm/s and to 1 under 150mm/s.
            movements = 3
            if curr_speed < 150:
                movements = 1
            elif curr_speed < 250:
                movements = 2

            # Back and forth movements to record the vibrations at constant speed in both direction
            name = f'vib_an{curr_angle:.2f}sp{curr_speed:.2f}'.replace('.', '_')
            accelerometer.start_recording(measurements_manager, name=name, append_time=True)
            for _ in range(movements):
                toolhead.move([mid_x + dX, mid_y + dY, z_height, E], curr_speed)
                toolhead.move([mid_x - dX, mid_y - dY, z_height, E], curr_speed)
            accelerometer.stop_recording()

            toolhead.dwell(0.3)
            toolhead.wait_moves()

        # Restore the previous acceleration values
    if old_mcr is not None:  # minimum_cruise_ratio found: Klipper >= v0.12.0-239
        gcode.run_script_from_command(
            f'SET_VELOCITY_LIMIT ACCEL={old_accel} MINIMUM_CRUISE_RATIO={old_mcr} SQUARE_CORNER_VELOCITY={old_sqv}'
        )
    else:  # minimum_cruise_ratio not found: Klipper < v0.12.0-239
        gcode.run_script_from_command(f'SET_VELOCITY_LIMIT ACCEL={old_accel} SQUARE_CORNER_VELOCITY={old_sqv}')
    toolhead.wait_moves()

    # Run post-processing
    ConsoleOutput.print('Machine vibrations profile generation...')
    ConsoleOutput.print('This may take some time (5-8min)')
    creator = st_process.get_graph_creator()
    creator.configure(motors_config_parser.kinematics, accel, motors_config_parser)
    st_process.run(measurements_manager)
    st_process.wait_for_completion()
