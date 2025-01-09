#!/usr/bin/env python3

import os
import sys
import time
import shutil
import logging
import argparse
import numpy as np
import json
import traceback
from scipy import stats

import custom_script
from custom_script import EXP_NAME
from custom_script import EVOLVER_PORT, OPERATION_MODE
from custom_script import STIR_INITIAL, TEMP_INITIAL

# Should not be changed
# vials to be considered/excluded should be handled
# inside the custom functions
VIALS = [x for x in range(16)]

SAVE_PATH = os.path.dirname(os.path.realpath(__file__))
EXP_DIR = os.path.join(SAVE_PATH, EXP_NAME)
OD_CAL_PATH = os.path.join(SAVE_PATH, 'od_cal.json')
TEMP_CAL_PATH = os.path.join(SAVE_PATH, 'temp_cal.json')
PUMP_CAL_PATH = os.path.join(SAVE_PATH, 'pump_cal.json')
JSON_PARAMS_FILE = os.path.join(SAVE_PATH, 'eVOLVER_parameters.json')

SIGMOID = 'sigmoid'
LINEAR = 'linear'
THREE_DIMENSION = '3d'

logger = logging.getLogger('eVOLVER')

paused = False

EVOLVER_NS = None

class EvolverSimulation:
    def __init__(self):
        self.exp_dir = SAVE_PATH

        # Time variables
        self.start_time = 0 # hours
        self.current_time = 0 # hours
        self.iteration_length = 1/60 # hours

    def exponential_growth(self, vial):
        """Simulate exponential growth."""
        self.OD[vial] *= np.exp(self.growth_rates[vial] * DELTA_T)
    
    def check_dilution(self, vial):
        """Check if dilution is needed."""
        lower, upper = self.dilution_thresholds[vial]
        if self.OD[vial] > upper:
            dilution_factor = lower / self.OD[vial]
            self.OD[vial] *= dilution_factor
            self.logs[vial].append((self.elapsed_time, 'Dilution', self.OD[vial]))
    
    def simulate_step(self):
        """Simulate one time step."""
        for vial in range(self.vials):
            self.exponential_growth(vial)
            self.check_dilution(vial)
        self.elapsed_time += DELTA_T
    
    def run_simulation(self):
        """Run the simulation for MAX_TIME."""
        while self.elapsed_time < MAX_TIME:
            self.simulate_step()
        self.save_logs()
    
    def save_logs(self):
        """Save logs to files."""
        for vial in range(self.vials):
            df = pd.DataFrame(self.logs[vial], columns=["Time", "Event", "OD"])
            df.to_csv(f"vial{vial}_log.csv", index=False)

    def fluid_command(self, MESSAGE):
        logger.debug('fluid command: %s' % MESSAGE)
        command = {'param': 'pump', 'value': MESSAGE,
                   'recurring': False ,'immediate': True}
        self.emit('command', command, namespace='/dpu-evolver')

    def update_chemo(self, data, vials, bolus_in_s, period_config, immediate = False):
        current_pump = data['config']['pump']['value']

        MESSAGE = {'fields_expected_incoming': 49,
                   'fields_expected_outgoing': 49,
                   'recurring': True,
                   'immediate': immediate,
                   'value': ['--'] * 48,
                   'param': 'pump'}

        for x in vials:
            # stop pumps if period is zero
            if period_config[x] == 0:
                # influx
                MESSAGE['value'][x] = '0|0'
                # efflux
                MESSAGE['value'][x + 16] = '0|0'
            else:
                # influx
                MESSAGE['value'][x] = '%.2f|%d' % (bolus_in_s[x], period_config[x])
                # efflux
                MESSAGE['value'][x + 16] = '%.2f|%d' % (bolus_in_s[x] * 2,
                                                        period_config[x])

        if MESSAGE['value'] != current_pump:
            logger.info('updating chemostat: %s' % MESSAGE)
            self.emit('command', MESSAGE, namespace = '/dpu-evolver')

    def stop_all_pumps(self, ):
        data = {'param': 'pump',
                'value': ['0'] * 48,
                'recurring': False,
                'immediate': True}
        logger.info('stopping all pumps')
        self.emit('command', data, namespace = '/dpu-evolver')

    def _create_file(self, vial, param, directory=None, defaults=None):
        if defaults is None:
            defaults = []
        if directory is None:
            directory = param
        file_name =  "vial{0}_{1}.txt".format(vial, param)
        file_path = os.path.join(EXP_DIR, directory, file_name)
        text_file = open(file_path, "w")
        for default in defaults:
            text_file.write(default + '\n')
        text_file.close()

    def initialize_exp(self, vials, log_name, quiet, verbose):

        logger.debug('creating data directories')
        os.makedirs(os.path.join(EXP_DIR, 'OD'))
        os.makedirs(os.path.join(EXP_DIR, 'temp'))
        os.makedirs(os.path.join(EXP_DIR, 'temp_config'))
        os.makedirs(os.path.join(EXP_DIR, 'pump_log'))
        os.makedirs(os.path.join(EXP_DIR, 'ODset'))
        os.makedirs(os.path.join(EXP_DIR, 'growthrate'))
        os.makedirs(os.path.join(EXP_DIR, 'chemo_config'))
        setup_logging(log_name, quiet, verbose)
        for x in vials:
            exp_str = "Experiment: {0} vial {1}, {2}".format(EXP_NAME,
                                                                x,
                                                        time.strftime("%c"))
            # make OD file
            self._create_file(x, 'OD', defaults=[exp_str])
            # make ODset file
            self._create_file(x, 'ODset',
                                defaults=[exp_str,
                                        "0,0"])
            # make growth rate file
            self._create_file(x, 'gr',
                                defaults=[exp_str,
                                        "0,0"],
                                directory='growthrate')
            # make chemostat file
            self._create_file(x, 'chemo_config',
                                defaults=["0,0,0",
                                        "0,0,0"],
                                directory='chemo_config')

    def save_data(self, data, elapsed_time, vials, parameter):
        if len(data) == 0:
            return
        for x in vials:
            file_name =  "vial{0}_{1}.txt".format(x, parameter)
            file_path = os.path.join(EXP_DIR, parameter, file_name)
            text_file = open(file_path, "a+")
            text_file.write("{0},{1}\n".format(elapsed_time, data[x]))
            text_file.close()

    def get_flow_rate(self):
        pump_cal = None
        with open(PUMP_CAL_PATH) as f:
            pump_cal = json.load(f)
        return pump_cal['coefficients']

    def calc_growth_rate(self, vial, gr_start, elapsed_time):
        ODfile_name =  "vial{0}_OD.txt".format(vial)
        # Grab Data and make setpoint
        OD_path = os.path.join(EXP_DIR, 'OD', ODfile_name)
        OD_data = np.genfromtxt(OD_path, delimiter=',')
        raw_time = OD_data[:, 0]
        raw_OD = OD_data[:, 1]
        raw_time = raw_time[np.isfinite(raw_OD)]
        raw_OD = raw_OD[np.isfinite(raw_OD)]

        # Trim points prior to gr_start
        trim_time = raw_time[np.nonzero(np.where(raw_time > gr_start, 1, 0))]
        trim_OD = raw_OD[np.nonzero(np.where(raw_time > gr_start, 1, 0))]

        # Take natural log, calculate slope
        log_OD = np.log(trim_OD)
        slope, intercept, r_value, p_value, std_err = stats.linregress(
            trim_time[np.isfinite(log_OD)],
            log_OD[np.isfinite(log_OD)])
        logger.debug('growth rate for vial %s: %.2f' % (vial, slope))

        # Save slope to file
        file_name =  "vial{0}_gr.txt".format(vial)
        gr_path = os.path.join(EXP_DIR, 'growthrate', file_name)
        text_file = open(gr_path, "a+")
        text_file.write("{0},{1}\n".format(elapsed_time, slope))
        text_file.close()

    def tail_to_np(self, path, window=10, BUFFER_SIZE=512):
        """
        Reads file from the end and returns a numpy array with the data of the last 'window' lines.
        Alternative to np.genfromtxt(path) by loading only the needed lines instead of the whole file.
        """
        f = open(path, 'rb')
        if window == 0:
            return []

        f.seek(0, os.SEEK_END)
        remaining_bytes = f.tell()
        size = window + 1  # Read one more line to avoid broken lines
        block = -1
        data = []

        while size > 0 and remaining_bytes > 0:
            if remaining_bytes - BUFFER_SIZE > 0:
                # Seek back one whole BUFFER_SIZE
                f.seek(block * BUFFER_SIZE, os.SEEK_END)
                # read BUFFER
                bunch = f.read(BUFFER_SIZE)
            else:
                # file too small, start from beginning
                f.seek(0, 0)
                # only read what was not read
                bunch = f.read(remaining_bytes)

            bunch = bunch.decode('utf-8')
            data.append(bunch)
            size -= bunch.count('\n')
            remaining_bytes -= BUFFER_SIZE
            block -= 1

        data = ''.join(reversed(data)).splitlines()[-window:]

        if len(data) < window:
            # Not enough data
            return np.asarray([])

        for c, v in enumerate(data):
            data[c] = v.split(',')

        try:
            data = np.asarray(data, dtype=np.float64)
            return data
        except ValueError:
            # It is reading the header
            return np.asarray([])

    def custom_functions(self, data, vials, elapsed_time):
        # load user script from custom_script.py
        mode = self.experiment_params['function'] if self.experiment_params else OPERATION_MODE
        if mode == 'turbidostat':
            custom_script.turbidostat(self, data, vials, elapsed_time)
        elif mode == 'chemostat':
            custom_script.chemostat(self, data, vials, elapsed_time)
        elif mode == 'growthcurve':
            custom_script.growth_curve(self, data, vials, elapsed_time)
        else:
            # try to load the user function
            # if failing report to user
            logger.info('user-defined operation mode %s' % mode)
            try:
                func = getattr(custom_script, mode)
                func(self, data, vials, elapsed_time)
            except AttributeError:
                logger.error('could not find function %s in custom_script.py' %
                            mode)
                print('Could not find function %s in custom_script.py '
                    '- Skipping user defined functions'%
                    mode)

    def stop_exp(self):
        logger.info('stopping experiment')
        print('stopping experiment')

def setup_logging(filename, quiet, verbose):
    if quiet:
        logging.basicConfig(level=logging.CRITICAL + 10)
    else:
        if verbose == 0:
            level = logging.INFO
        elif verbose >= 1:
            level = logging.DEBUG
        logging.basicConfig(format='%(asctime)s - %(name)s - [%(levelname)s] '
                            '- %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            filename=filename,
                            level=level)

if __name__ == '__main__':

    #changes terminal tab title in OSX
    print('\x1B]0;eVOLVER EXPERIMENT: PRESS Ctrl-C TO PAUSE\x07')

    experiment_params = None
    if os.path.exists(JSON_PARAMS_FILE):
        with open(JSON_PARAMS_FILE) as f:
            experiment_params = json.load(f)
    evolver_ip = experiment_params['ip'] if experiment_params is not None else options.ip_address
    if evolver_ip is None:
        logger.error('No IP address found. Please provide on the command line or through the GUI.')
        parser.print_help()
        sys.exit(2)

    socketIO = SocketIO(evolver_ip, EVOLVER_PORT)
    EVOLVER_NS = socketIO.define(EvolverNamespace, '/dpu-evolver')

    # start by stopping any existing chemostat
    EVOLVER_NS.stop_all_pumps()
    #
    EVOLVER_NS.start_time = EVOLVER_NS.initialize_exp(VIALS,
                                                      experiment_params,
                                                      options.log_name,
                                                      options.quiet,
                                                      options.verbose,
                                                      )

    # Using a non-blocking stream reader to be able to listen
    # for commands from the electron app. 
    nbsr = NBSR(sys.stdin)
    paused = False

    # logging setup

    reset_connection_timer = time.time()
    while True:        
        try:
            # infinite loop

            # check if a message has come in from the DPU
            message = nbsr.readline()
            if 'stop-script' in message:
                logger.info('Stop message received - halting all pumps');
                EVOLVER_NS.stop_exp()
                socketIO.disconnect()
            if 'pause-script' in message:
                print('Pausing experiment', flush = True)
                logger.info('Pausing experiment in dpu')
                paused = True
                EVOLVER_NS.stop_exp()
                socketIO.disconnect()
                
            if 'continue-script' in message:
                print('Restarting experiment', flush = True)
                logger.info('Restarting experiment')
                paused = False
                socketIO.connect()

            if not paused:
                    socketIO.wait(seconds=0.1)
                    if time.time() - reset_connection_timer > 3600 and not paused:
                        # reset connection to avoid buildup of broadcast
                        # messages (unlikely but could happen for very long
                        # experiments with slow dpu code/computer)
                        logger.info('resetting connection to eVOLVER to avoid '
                                    'potential buildup of broadcast messages')
                        socketIO.disconnect()
                        socketIO.connect()
                        reset_connection_timer = time.time()
        except KeyboardInterrupt:
            try:
                print('Ctrl-C detected, pausing experiment')
                logger.warning('interrupt received, pausing experiment')
                EVOLVER_NS.stop_exp()
                # stop receiving broadcasts
                socketIO.disconnect()
                while True:
                    key = input('Experiment paused. Press enter key to restart '
                                ' or hit Ctrl-C again to terminate experiment')
                    logger.warning('resuming experiment')
                    # no need to have something like "restart_chemo" here
                    # with the new server logic
                    socketIO.connect()
                    break
            except KeyboardInterrupt:
                print('Second Ctrl-C detected, shutting down')
                logger.warning('second interrupt received, terminating '
                                'experiment')
                EVOLVER_NS.stop_exp()
                print('Experiment stopped, goodbye!')
                logger.warning('experiment stopped, goodbye!')
                break
        except Exception as e:
            logger.critical('exception %s stopped the experiment' % str(e))
            print('error "%s" stopped the experiment' % str(e))
            traceback.print_exc(file=sys.stdout)
            EVOLVER_NS.stop_exp()
            print('Experiment stopped, goodbye!')
            logger.warning('experiment stopped, goodbye!')
            break

    # stop experiment one last time
    # covers corner case where user presses Ctrl-C twice quickly
    socketIO.connect()
    EVOLVER_NS.stop_exp()
