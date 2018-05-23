#!/usr/bin/env python
import base64
from datetime import datetime, timedelta
import pfycat
import logging
import os
import os.path
import paho.mqtt.client as mqtt
import subprocess
import tempfile
import threading

FORMAT = '%(asctime)s %(message)s'
logging.basicConfig(format=FORMAT, level=logging.INFO)
logger = logging.getLogger('mqtt_record')

PRE = 10	# before the event
OPEN = 30	# wait for door to close
POST = 10	# after the event
DELAY = 2	# wait for camera pictures
FPS = 30
MQTT_HOSTNAME = "localhost"
MQTT_USERNAME = "username"
MQTT_PASSWORD = "password"
CAMERA_IMAGES_DIR = "/tank/security/zoneminder/events/Entry"
DEST_ADDRESSES = ['test@gmail.com']

current_event = {}

def on_connect(client, userdata, flags, rc):
	logger.info("Connected to MQTT with result code %s" % (rc,))
	client.subscribe('/frontdoor')	# where door changes will happen
def on_message(client, userdata, msg):
	""" The door opened or closed """
	# cancel the open door timer or after-closing timer
	if 'timer' in current_event:
		current_event['timer'].cancel()
		del current_event['timer']
	if msg.payload == "OPEN":
		logger.info("Door opened")
		open_time = datetime.utcnow()
		start = open_time - timedelta(seconds=PRE)
		if 'start' not in current_event:
			logger.info("Starting fresh event")
			current_event['start'] = start
		else:
			logger.info("Continuing previous capture")
		if 'stop' in current_event:
			logger.info("Continuing previously finished capture")
			del current_event['stop']
		current_event['timer'] = threading.Timer(OPEN, still_open)
		current_event['timer'].start()
	if msg.payload == "CLOSED" and 'start' in current_event:
		logger.info("Door closed")
		closed_time = datetime.utcnow()
		current_event['stop'] = closed_time + timedelta(seconds=POST)
		# wait for the camera to load the next frames
		current_event['timer'] = threading.Timer(POST+DELAY, captured_event)
		current_event['timer'].start()
def still_open():
	""" The door stayed open for too long, give up """
	if 'start' not in current_event:
		logger.warning("Open Door timer triggered without a capture?")
		return
	logger.info("Door is open for a long time")
	current_event['stop'] = datetime.utcnow()
	current_event['timer'] = threading.Timer(DELAY, captured_event)
	current_event['timer'].start()

def captured_event():
	logger.info("Saving capture")
	images = get_images(current_event['start'], current_event['stop'])
	logger.info("Collating %s frames" % (len(images),))
	current_event.clear()
	video = convert_to_video(images)
	logger.info("Encoded video %s" % (video,))
	url = upload_video(video)
	logger.info("Uploaded video to %s" % (url,))
	notify_recipients(url)

def get_dir_timeparts(pathname):
	timepath = pathname[len(CAMERA_IMAGES_DIR)+1:]
	timestamp_parts = timepath.split('/')
	timestamp_parts[0] = '20' + timestamp_parts[0]
	timestamp_parts = [int(p) for p in timestamp_parts]
	while len(timestamp_parts) < 3:
		timestamp_parts.append(1)
	return timestamp_parts

def get_dir_datetime(pathname):
	timestamp_parts = get_dir_timeparts(pathname)
	dir_datetime = datetime(*timestamp_parts, tzinfo=None)
	return dir_datetime

def get_time_dirs(start, stop):
	dirs = []
	for (dirpath, dirnames, filenames) in os.walk(CAMERA_IMAGES_DIR):
		if len(dirnames) == 0 and len(filenames) > 0:
			# navigated to a proper time dir
			dirs.append(dirpath)
			next
		# still digging through directories
		interesting_dirnames = []
		for dirname in dirnames:
			if dirname.startswith('.'):
				continue
			pathname = os.path.join(dirpath, dirname)
			dir_datetime = get_dir_datetime(pathname)
			if dir_datetime < start:
				# only keep the first directory before the start
				interesting_dirnames = []
			if dir_datetime > stop:
				# don't investigate folders after the stop time
				next
			timestamp_parts = get_dir_timeparts(pathname)
			if len(timestamp_parts) == 6:
				# found the deepest we need to go
				dirs.append(pathname)
			else:
				# must go deeper
				interesting_dirnames.append(dirname)
		# save the directories to dig into
		del dirnames[:]
		dirnames.extend(interesting_dirnames)
	return dirs

def get_dir_images(start, stop, dir):
	images = []
	for filename in sorted(os.listdir(dir)):
		if not filename.endswith('.jpg'): continue
		path = os.path.join(dir, filename)
		mtime = os.path.getmtime(path)
		mtime_date = datetime.utcfromtimestamp(mtime)
		if mtime_date >= start and mtime_date <= stop:
			images.append(path)
	return images
def get_images(start, stop):
	images = []
	dirs = get_time_dirs(start, stop)
	logger.info("Locating directories for pictures between %s and %s: %s" % (start, stop, dirs))
	for dir in dirs:
		images.extend(get_dir_images(start, stop, dir))
	return images

def convert_to_video(images):
	dirname = tempfile.mkdtemp(prefix='mqtt_record_')
	for i,image in enumerate(images):
		os.symlink(image, os.path.join(dirname, '%06d.jpg' % (i,)))
	output = os.path.join(dirname, 'output.mp4')
	#ffmpeg -framerate 15 -i %05d.jpg -c:v libx264 -pix_fmt yuv420p practice-20150720.mp4
	cmd = ['/usr/bin/ffmpeg',
	       '-framerate', str(FPS),
	       '-i', os.path.join(dirname, '%06d.jpg'),
	       '-c:v', 'libx264',
	       '-pix_fmt', 'yuv420p',
	       output]
	logger.info("Calling %s" % (cmd,))
	rc = subprocess.call(cmd)
	logger.info("Converted with rc:%s" % (rc,))
	return output

def upload_video(video):
	logger.info("Uploading %s to gfycat" % (video, ))
	upload = pfycat.Client().upload(video)
	url = 'https://gfycat.com/%s' % (upload['gfyname'],)
	logger.info("Uploaded %s to %s" % (video, url))
	return url

def notify_recipients(url):
	dir = os.path.dirname(os.path.abspath(__file__))
	script = os.path.join(dir, 'notify')
	for dest in DEST_ADDRESSES:
		logger.info("Notifying %s" % (dest,))
		subprocess.call([script, dest, url])

#start = datetime(2016, 1, 30, 6, 19, 58)
#stop = datetime(2016,1,30,6,20,4)
#print('\n'.join(get_images(start, stop)))

if __name__ == '__main__':
	logger.info("Starting to connect to %s" % (MQTT_HOSTNAME,))
	client = mqtt.Client()
	client.on_connect = on_connect
	client.on_message = on_message
	client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
	client.connect(MQTT_HOSTNAME, 1883, 60)
	client.loop_forever()

