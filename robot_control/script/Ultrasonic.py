import time
from .Motor import *
from gpiozero import DistanceSensor
from .servo import *
from .PCA9685 import PCA9685
import random
trigger_pin = 27
echo_pin    = 22
sensor = DistanceSensor(echo=echo_pin, trigger=trigger_pin ,max_distance=3)
class Ultrasonic:
    def __init__(self):        
        pass
    def get_distance(self):     # get the measurement results of ultrasonic module,with unit: cm
        distance_cm = sensor.distance * 100
        return  int(distance_cm)
    
    def run(self):
        self.PWM = Motor()
        self.pwm_S = Servo()
        while True:
            M = self.get_distance()
            if M <= 20:
                self.PWM.setMotorModel(-1000, -1000, -1000, -1000)
                time.sleep(0.3)  
                if 50 >= random.randint(1, 100):
                    self.PWM.setMotorModel(-2000, -2000, 2000, 2000)
                else:
                    self.PWM.setMotorModel(2000, 2000, -2000, -2000)
                time.sleep(0.3)  

            elif 20 < M <= 30:
                self.PWM.setMotorModel(0, 0, 0, 0)
                time.sleep(0.2)
                if 50 >= random.randint(1, 100):
                    self.PWM.setMotorModel(-2000, -2000, 2000, 2000)
                else:
                    self.PWM.setMotorModel(2000, 2000, -2000, -2000)
                time.sleep(0.3)
            else:  
                self.PWM.setMotorModel(1000, 1000, 1000, 1000)
        
            
        
ultrasonic=Ultrasonic()              
# Main program logic follows:
if __name__ == '__main__':
    print ('Program is starting ... ')
    try:
        ultrasonic.run()
    except KeyboardInterrupt:  # When 'Ctrl+C' is pressed, the child program destroy() will be  executed.
        PWM.setMotorModel(0,0,0,0)
        ultrasonic.pwm_S.setServoPwm('0',90)
