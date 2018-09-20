import copy

import numpy as np
import torch

from utils.dotdic import DotDic
from modules.dru import DRU


class Arena:
	def __init__(self, opt, game):
		self.opt = opt
		self.game = game

	def create_episode(self):
		opt = self.opt
		episode = DotDic({})
		episode.steps = torch.zeros(opt.bs)
		episode.ended = torch.zeros(opt.bs)
		episode.r = torch.zeros(opt.bs, opt.game_nagents)
		episode.comm_per = torch.zeros(opt.bs)
		episode.comm_count = 0
		episode.non_comm_count = 0
		episode.step_records = []

		return episode

	def create_step_record(self):
		opt = self.opt
		record = DotDic({})
		record.s_t = None
		record.r_t = torch.zeros(opt.bs, opt.game_nagents)
		record.terminal = torch.zeros(opt.bs)

		# Track actions at time t per agent
		record.a_t = torch.zeros(opt.bs, opt.game_nagents, dtype=torch.long)
		if not opt.model_dial:
			record.a_comm_t = torch.zeros(opt.bs, opt.game_nagents, dtype=torch.long)

		# Track messages sent at time t per agent
		if opt.comm_enabled:
			comm_dtype = opt.model_dial and torch.float or torch.long
			record.comm = torch.zeros(opt.bs, opt.game_nagents, opt.game_comm_bits, dtype=comm_dtype)
			if opt.model_dial and opt.model_target:
				record.comm_target = record.comm.clone()
				
		record.d_comm = torch.zeros(opt.bs, opt.game_nagents, opt.game_comm_bits)

		# Track hidden state per time t per agent
		record.hidden = torch.zeros(opt.bs, opt.game_nagents, opt.model_rnn_size)
		record.hidden_target = torch.zeros(opt.bs, opt.game_nagents, opt.model_rnn_size)

		# Track Q(a_t) and Q(a_max_t) per agent
		record.q_a_t = torch.zeros(opt.bs, opt.game_nagents)
		record.q_a_max_t = torch.zeros(opt.bs, opt.game_nagents)

		# Track Q(m_t) and Q(m_max_t) per agent
		if not opt.model_dial:
			record.q_comm_t = torch.zeros(opt.bs, opt.game_nagents)
			record.q_comm_max_t = torch.zeros(opt.bs, opt.game_nagents)

		return record

	def run_episode(self, agents, train_mode=False):
		opt = self.opt
		game = self.game
		game.reset()

		step = 0
		episode = self.create_episode()
		s_t = game.get_state()
		episode.step_records.append(self.create_step_record())
		episode.step_records[-1].s_t = s_t
		episode_steps = train_mode and opt.nsteps + 1 or opt.nsteps
		while step < episode_steps and episode.ended.sum() < opt.bs:
			episode.step_records.append(self.create_step_record())

			for i in range(1, opt.game_nagents + 1):
				# Get received messages per agent per batch
				agent = agents[i]
				agent_idx = i - 1
				comm = None
				if opt.comm_enabled:
					comm = episode.step_records[step].comm.clone()
					comm_limited = self.game.get_comm_limited(step, agent_idx)
					if comm_limited is not None:
						comm_lim = torch.zeros(opt.bs, 1, opt.game_comm_bits)
						for b in range(opt.bs):
							comm_lim[b] = comm[b][comm_limited[b] - 1]
						comm = comm_lim
					else:
						comm[:, agent_idx].zero_()

				# Get prev action per batch
				prev_action = None
				if opt.model_action_aware:
					prev_action = torch.zeros(opt.bs, dtype=torch.long)
					if step > 1:
						prev_action = episode.step_records[step - 1].a_t[:, agent_idx]
					if not opt.model_dial:
						prev_message = torch.zeros(opt.bs, dtype=torch.long)
						if step > 1:
							prev_message = episode_step_records[step - 1].a_comm_t[:, agent_idx]
						prev_action = (prev_action, prev_message)

				# Batch agent index for input into model
				batch_agent_index = torch.zeros(opt.bs, dtype=torch.long).fill_(agent_idx)

				# @todo: turn into a dictionary

				agent_inputs = {
					's_t': s_t[:, agent_idx],
					'messages': comm,
					'hidden': episode.step_records[step].hidden[:, agent_idx], # Hidden state
					'prev_action': prev_action,
					'agent_index': batch_agent_index
				}

				# Compute model output (Q function + message bits)
				hidden_t, q_t = agent.model(**agent_inputs)
				episode.step_records[step + 1].hidden[:, agent_idx] = hidden_t.squeeze()

				# Choose next action and comm using eps-greedy selector
				(action, action_value), (comm_vector, comm_action, comm_value) = \
					agent.select_action_and_comm(step, q_t, eps=opt.eps, train_mode=train_mode)

				# Store action + comm
				episode.step_records[step].a_t[:, agent_idx] = action
				episode.step_records[step].q_a_t[:, agent_idx] = action_value
				episode.step_records[step + 1].comm[:, agent_idx] = comm_vector
				if not opt.model_dial:
					episode.step_records[step].a_comm_t[:, agent_idx] = comm_action
					episode.step_records[step].q_comm_t[:, agent_idx] = comm_value

			# Update game to next step
			a_t = episode.step_records[step].a_t
			episode.step_records[step].r_t, episode.step_records[step].terminal = \
				self.game.step(a_t)

			# Accumulate steps
			if step < opt.nsteps:
				for b in range(opt.bs):
					if not episode.ended[b]:
						episode.steps[b] = episode.steps[b] + 1
						episode.r[b] = episode.r[b] + episode.step_records[step].r_t[b]

					if episode.step_records[step].terminal[b]:
						episode.ended[b] = 1
			
			# Target-network forward pass
			if opt.model_target and train_mode:
				for i in range(1, opt.game_nagents):
					agent_target = agents[i]
					agent_idx = i - 1

					comm_target = agent_inputs.get('messages', None)

					if opt.comm_enabled and opt.model_dial:
						comm_target = episode.step_records[step].comm_target.clone()
						comm_limited = self.game.get_comm_limited(step, agent_idx)
						if comm_limited is not None:
							comm_lim = torch.zeros(opt.bs, 1, opt.game_comm_bits)
							for b in range(opt.bs):
								comm_lim[b] = comm_target[b][comm_limited[b] - 1]
							comm_target = comm_lim
						else:
							comm_target[:, agent_idx].zero_()

					agent_target_inputs = copy.copy(agent_inputs)
					agent_target_inputs['messages'] = comm_target
					hidden_target_t, q_target_t = agent.model_target(**agent_target_inputs)

					# Choose next arg max action and comm
					(action, action_value), (comm_vector, comm_action, comm_value) = \
						agent.select_action_and_comm(step, q_t, eps=0, train_mode=True)

					# save target actions, comm, and q_a_t, q_a_max_t
					episode.step_records[step].q_a_max_t[:, agent_idx] = action_value
					episode.step_records[step + 1].comm_target[:, agent_idx] = comm_vector
					if not opt.model_dial:
						episode.step_records[step].q_comm_max_t[:, agent_index] = comm_value

			# Update step
			step = step + 1
			if episode.ended.sum().item() < opt.bs:
				episode.step_records[step].s_t = self.game.get_state()

			# import pdb; pdb.set_trace()
			print('finished step', step - 1, 'reward:', episode.r.mean().item())

		return episode

	def train(self, agents):
		opt = self.opt
		for e in range(opt.nepisodes):
			# run episode
			episode = self.run_episode(agents, train_mode=True)

			# backprop Q values
			

			# backprop message gradients

			# update parameters

			pass
