# GPU based A3C
# haiyinpiao@qq.com

import numpy as np
import tensorflow as tf
import os

import gym, time, random

# import threading

import multiprocessing as mp

from keras.models import *
from keras.layers import *
from keras import backend as K

#log and visualization.
import matplotlib as mpl
import matplotlib.pyplot as plt
import datetime
start = time.time()

a_time = mp.Queue()
a_reward = mp.Queue()

#multithreading for brain
from threading import Thread

def log_reward( R ):
	a_time.put( time.time() - start )
	a_reward.put( R )

#-- constants
ENV = 'CartPole-v0'

RUN_TIME = 30
THREADS = 16
THREAD_DELAY = 0.001
PREDICTORS = 1
TRAINERS = 1

GAMMA = 0.99

N_STEP_RETURN = 8
GAMMA_N = GAMMA ** N_STEP_RETURN

EPS_START = 0.4
EPS_STOP  = .15
EPS_STEPS = 75000

MIN_BATCH = 1
LEARNING_RATE = 5e-3

LOSS_V = .5			# v loss coefficient
LOSS_ENTROPY = .01 	# entropy coefficient

#---------
class Brain:
	def __init__(self):
		
		self.session = tf.Session()
		K.set_session(self.session)
		K.manual_variable_initialization(True)

		self.model = self._build_model()
		self.graph = self._build_graph(self.model)

		self.session.run(tf.global_variables_initializer())
		self.default_graph = tf.get_default_graph()

		self.default_graph.finalize()	# avoid modifications

		# multiprocess global sample queue for batch traning.
		self._train_queue = mp.Queue()
		self._train_lock = mp.Lock()

		# multiprocess global state queue for action predict
		self._predict_queue = mp.Queue()
		self._predict_lock = mp.Lock()

		self._predictors = []
		self._trainers = []

	def _build_model(self):

		l_input = Input( batch_shape=(None, NUM_STATE) )
		l_dense = Dense(16, activation='relu')(l_input)

		out_actions = Dense(NUM_ACTIONS, activation='softmax')(l_dense)
		out_value   = Dense(1, activation='linear')(l_dense)

		model = Model(inputs=[l_input], outputs=[out_actions, out_value])
		model._make_predict_function()	# have to initialize before threading

		return model

	def _build_graph(self, model):
		s_t = tf.placeholder(tf.float32, shape=(None, NUM_STATE))
		a_t = tf.placeholder(tf.float32, shape=(None, NUM_ACTIONS))
		r_t = tf.placeholder(tf.float32, shape=(None, 1)) # not immediate, but discounted n step reward
		
		p, v = model(s_t)

		log_prob = tf.log( tf.reduce_sum(p * a_t, axis=1, keep_dims=True) + 1e-10)
		advantage = r_t - v

		loss_policy = - log_prob * tf.stop_gradient(advantage)									# maximize policy
		loss_value  = LOSS_V * tf.square(advantage)												# minimize value error
		entropy = LOSS_ENTROPY * tf.reduce_sum(p * tf.log(p + 1e-10), axis=1, keep_dims=True)	# maximize entropy (regularization)

		loss_total = tf.reduce_mean(loss_policy + loss_value + entropy)

		optimizer = tf.train.RMSPropOptimizer(LEARNING_RATE, decay=.99)
		minimize = optimizer.minimize(loss_total)

		return s_t, a_t, r_t, minimize

	def predict(self, s):
		with self.default_graph.as_default():
			p, v = self.model.predict(s)		
			return p, v

	def predict_p(self, s):
		with self.default_graph.as_default():
			p, v = self.model.predict(s)		
			return p

	def predict_v(self, s):
		with self.default_graph.as_default():
			p, v = self.model.predict(s)		
			return v

	def add_predictor(self):
		self._predictors.append(ThreadPredictor(self, len(self._predictors)))
		self._predictors[-1].start()

	def add_trainer(self):
		self._trainers.append(ThreadTrainer(self, len(self._trainers)))
		self._trainers[-1].start()

class ThreadPredictor(Thread):
	def __init__(self, brain, id):
		super(ThreadPredictor, self).__init__()
		self.setDaemon(True)

		self._id = id
		self._brain = brain
		self.stop_signal = False

	def batch_predict(self):
		global envs

		if self._brain._predict_queue.qsize() < MIN_BATCH:	# more thread could have passed without lock
			time.sleep(0)
			return
				 									# we can't yield inside lock
		# if self._brain._predict_queue.empty():
		# 	return

		i = 0
		id = []
		s = []
		while not self._brain._predict_queue.empty():
			id_, s_ = self._brain._predict_queue.get()
			if i==0:
				s = s_
			else:
				s = np.row_stack((s, s_))
			id.append(id_)
			i += 1

		if s == []:
			return

		p = self._brain.predict_p(np.array(s))

		for j in range(i):
			if id[j] < len(envs):
				envs[id[j]].agent.wait_q.put(p[j])

	def run(self):
		while not self.stop_signal:
			self.batch_predict()
			
	def stop(self):
		self.stop_signal = True

class ThreadTrainer(Thread):
	def __init__(self, brain, id):
		super(ThreadTrainer, self).__init__()
		self.setDaemon(True)

		self._id = id
		self._brain = brain
		self.stop_signal = False

	def batch_train(self):
		if self._brain._train_queue.qsize() < MIN_BATCH:	# more thread could have passed without lock
			time.sleep(0)
			return 									# we can't yield inside lock

		if self._brain._train_queue.empty():
			return

		i = 0
		s = []
		while not self._brain._train_queue.empty():
			s_, a_, r_, s_next_, s_mask_ = self._brain._train_queue.get()
			if i==0:
				s, a, r, s_next, s_mask = s_, a_, r_, s_next_, s_mask_
			else:
				s = np.row_stack((s, s_))
				a = np.row_stack((a, a_))
				r = np.row_stack((r, r_))
				s_next = np.row_stack((s_next, s_next_))
				s_mask = np.row_stack( (s_mask, s_mask_) )
			i += 1
		if s == []:
			return

		if len(s) > 100*MIN_BATCH: print("Optimizer alert! Minimizing train batch of %d" % len(s))

		v = self._brain.predict_v(s_next)
		r = r + GAMMA_N * v * s_mask	# set v to 0 where s_ is terminal state
		
		s_t, a_t, r_t, minimize = self._brain.graph
		self._brain.session.run(minimize, feed_dict={s_t: s, a_t: a, r_t: r})

	def run(self):
		while not self.stop_signal:
			self.batch_train()

	def stop(self):
		self.stop_signal = True

#---------
frames = 0
class Agent:
	def __init__(self, id, eps_start, eps_end, eps_steps, predict_queue, predict_lock, train_queue, train_lock):
		self.id = id
		self.eps_start = eps_start
		self.eps_end   = eps_end
		self.eps_steps = eps_steps

		self.memory = []	# used for n_step return
		self.R = 0.

		# for predicted nn output dispatching
		self.wait_q = mp.Queue(maxsize=1)
		self._predict_queue = predict_queue
		self._predict_lock = predict_lock

		# for training
		self._train_queue = train_queue
		self._train_lock = train_lock

	def getEpsilon(self):
		if(frames >= self.eps_steps):
			return self.eps_end
		else:
			return self.eps_start + frames * (self.eps_end - self.eps_start) / self.eps_steps	# linearly interpolate

	def act(self, s):
		eps = self.getEpsilon()			
		global frames; frames = frames + 1

		s = np.array([s])
		# put the state in the prediction q
		self._predict_queue.put((self.id, s))
		# wait for the prediction to come back
		p = self.wait_q.get()
		a = np.random.choice(NUM_ACTIONS, p=p)
		if random.random() < eps:
			a = random.randint(0, NUM_ACTIONS-1)
		return a
	
	def train(self, s, a, r, s_):

		def train_push(s, a, r, s_):
			if s_ is None:
				s_next = NONE_STATE
				s_mask = 0.
			else:
				s_next = s_
				s_mask = 1.
			s = np.array([s])
			a = np.array([a])
			r = np.array([r])
			s_next = np.array([s_next])
			s_mask = np.array([s_mask])
			self._train_queue.put( (s, a, r, s_next, s_mask) )

		def get_sample(memory, n):
			s, a, _, _  = memory[0]
			_, _, _, s_ = memory[n-1]

			return s, a, self.R, s_

		a_cats = np.zeros(NUM_ACTIONS)	# turn action into one-hot representation
		a_cats[a] = 1 

		self.memory.append( (s, a_cats, r, s_) )

		self.R = ( self.R + r * GAMMA_N ) / GAMMA

		if s_ is None:
			while len(self.memory) > 0:
				n = len(self.memory)
				s, a, r, s_ = get_sample(self.memory, n)
				train_push(s, a, r, s_)

				self.R = ( self.R - self.memory[0][2] ) / GAMMA
				self.memory.pop(0)
			self.R = 0

		if len(self.memory) >= N_STEP_RETURN:
			s, a, r, s_ = get_sample(self.memory, N_STEP_RETURN)
			train_push(s, a, r, s_)

			self.R = self.R - self.memory[0][2]
			self.memory.pop(0)
		
#---------
class Environment(mp.Process):
	stop_signal = False

	def __init__(self, id, predict_queue, predict_lock, train_queue, train_lock, render=False, eps_start=EPS_START, eps_end=EPS_STOP, eps_steps=EPS_STEPS, train=True):
		mp.Process.__init__(self)

		self.id = id
		self.render = render
		self.env = gym.make(ENV)
		self.agent = Agent(id, eps_start, eps_end, eps_steps, predict_queue, predict_lock, train_queue, train_lock)
		self._exit_flag = mp.Value('i', 0)
		self._train = train

	def runEpisode(self):
		s = self.env.reset()

		R = 0
		while True:      
			time.sleep(THREAD_DELAY)
			if self.render:
				self.env.render()
			a = self.agent.act(s)
			s_, r, done, info = self.env.step(a)

			if done: # terminal state
				s_ = None

			if self._train:
				self.agent.train(s, a, r, s_)

			s = s_
			R += r

			if done: #or self.stop_signal:
				break

		log_reward( R )
		print("Total R:", R)

	def run(self):
		while not self._exit_flag.value:
			self.runEpisode()

	def stop(self):
		self._exit_flag.value = 1

#-- main
env = gym.make(ENV)
NUM_STATE = env.observation_space.shape[0]
NUM_ACTIONS = env.action_space.n
NONE_STATE = np.zeros(NUM_STATE)

brain = Brain()	# brain is global in A3C
for i in range(PREDICTORS):
	brain.add_predictor()
for i in range(TRAINERS):
	brain.add_trainer()

# env_test = Environment(id=-1, predict_queue=brain._predict_queue, predict_lock=brain._predict_lock, train_queue=brain._train_queue, train_lock=brain._train_lock, render=True, eps_start=0., eps_end=0., train=False)
envs = [Environment(id=i, predict_queue=brain._predict_queue, predict_lock=brain._predict_lock, train_queue=brain._train_queue, train_lock=brain._train_lock) for i in range(THREADS)]

for e in envs:
	e.start()

time.sleep(RUN_TIME)

#plot rewards
# time_series, reward_series = [], []
# while not a_time.empty():
# 	time_series.append(a_time.get())
# 	reward_series.append(a_reward.get())

# plt.plot( time_series, reward_series )
# plt.show()

for e in envs:
	e.stop()
for e in envs:
	e.terminate()
	e.join()

for p in brain._predictors:
	p.stop()
for t in brain._trainers:
	t.stop()
for p in brain._predictors:
	p.join()
	brain._predictors.pop()
for t in brain._trainers:
	t.join()
	brain._trainers.pop()

print("Training finished")
# env_test.run()