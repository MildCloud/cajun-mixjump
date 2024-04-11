"""Computes leg states based on sinusoids and phase offsets."""
import copy
from typing import Any

from ml_collections import ConfigDict
from isaacgym.torch_utils import to_torch
import torch
import numpy as np


class PhaseGaitGenerator:
  """Computes desired gait based on leg phases."""
  def __init__(self, robot: Any, gait_config: ConfigDict):
    """Initializes the gait generator.
    Each gait is parameterized by 3 set of parameters:
      The _stepping frequency_: controls how fast the gait progresses.
      The _offset_: a 4-dim vector representing the offset from a standard
        gait cycle. In a standard gait cycle, each gait cycle starts in stance
        and ends in swing.
      The _swing ratio_: the percentage of air phase in each gait.
    """
    self._robot = robot
    self._num_envs = self._robot.num_envs
    self._device = self._robot._device
    self._config = ConfigDict()

    pronk_initial_offset = to_torch(gait_config.pronk_initial_offset, device=self._device)
    self._config.pronk_initial_offset = torch.stack([pronk_initial_offset] * self._num_envs, dim=0)
    pronk_swing_ratio = to_torch(gait_config.pronk_swing_ratio, device=self._device)
    self._pronk_swing_cutoff = torch.ones((self._num_envs, 4), device=self._device) * 2 * torch.pi * (1 - pronk_swing_ratio)

    bound_initial_offset = to_torch(gait_config.bound_initial_offset, device=self._device)
    self._config.bound_initial_offset = torch.stack([bound_initial_offset] * self._num_envs, dim=0)
    bound_swing_ratio = to_torch(gait_config.bound_swing_ratio, device=self._device)
    self._bound_swing_cutoff = torch.ones((self._num_envs, 4), device=self._device) * 2 * torch.pi * (1 - bound_swing_ratio)
    
    self._config.stepping_frequency = gait_config.stepping_frequency
    self.reset()

  def reset(self):
    rand_gait = torch.where(torch.rand(self._num_envs) < 0.5, True, False)
    rand_gait = torch.stack([rand_gait] * 4, dim=1).to(self._device)
    self._initial_offset = torch.where(rand_gait, self._config.pronk_initial_offset, self._config.bound_initial_offset)
    self._current_phase = self._initial_offset
    self._swing_cutoff = torch.where(rand_gait, self._pronk_swing_cutoff, self._bound_swing_cutoff)
    self._stepping_frequency = torch.ones(self._num_envs, device=self._device) * self._config.stepping_frequency
    
    self._prev_frame_robot_time = self._robot.time_since_reset
    self._first_stance_seen = torch.zeros((self._num_envs, 4),
                                          dtype=torch.bool,
                                          device=self._device)
    self._cycle_count = torch.zeros((self._num_envs), device=self._device)

  def reset_idx(self, env_ids):
    rand_gait = torch.rand(1)
    if rand_gait[0] < 0.5:
      self._initial_offset[env_ids] = self._config.pronk_initial_offset[0]
      self._current_phase[env_ids] = self._initial_offset[env_ids]
      self._swing_cutoff[env_ids] = self._pronk_swing_cutoff[0]
    else:
      self._initial_offset[env_ids] = self._config.bound_initial_offset[0]
      self._current_phase[env_ids] = self._initial_offset[env_ids]
      self._swing_cutoff[env_ids] = self._bound_swing_cutoff[0]

    self._stepping_frequency[env_ids] = self._config.stepping_frequency
    self._prev_frame_robot_time[env_ids] = self._robot.time_since_reset[
        env_ids]
    self._first_stance_seen[env_ids] = 0
    self._cycle_count[env_ids] = 0

  def update(self):
    current_robot_time = self._robot.time_since_reset
    delta_t = current_robot_time - self._prev_frame_robot_time
    self._prev_frame_robot_time = current_robot_time
    self._current_phase += 2 * torch.pi * self._stepping_frequency[:, None] * delta_t[:, None]

    true_phase = self._current_phase[:, 0] - self._initial_offset[:, 0]
    stack_true_phase = torch.stack([true_phase, true_phase, true_phase, true_phase], dim=1)
    rand_gait = torch.where(torch.rand(self._num_envs) < 0.5, True, False)
    rand_gait = torch.stack([rand_gait] * 4, dim=1).to(self._device)
    rand_initial_offset = torch.where(rand_gait, self._config.pronk_initial_offset, self._config.bound_initial_offset)
    rand_swing_cutoff = torch.where(rand_gait, self._pronk_swing_cutoff, self._bound_swing_cutoff)

    self._initial_offset = torch.where(stack_true_phase > 2*torch.pi, rand_initial_offset, self._initial_offset)
    self._current_phase = torch.where(stack_true_phase > 2*torch.pi, self._initial_offset, self._current_phase)
    self._swing_cutoff = torch.where(stack_true_phase > 2*torch.pi, rand_swing_cutoff, self._swing_cutoff)
    self._cycle_count = torch.where(true_phase > 2*torch.pi, self._cycle_count+1, self._cycle_count)

  @property
  def desired_contact_state(self):
    modulated_phase = torch.remainder(self._current_phase + 2 * torch.pi,
                                      2 * torch.pi)
    raw_contact = torch.where(modulated_phase > self._swing_cutoff, False,
                              True)
    # print(f"Raw constact: {raw_contact}")
    self._first_stance_seen = torch.logical_or(self._first_stance_seen,
                                               raw_contact)
    return torch.where(self._first_stance_seen, raw_contact,
                       torch.ones_like(raw_contact))

  @property
  def desired_contact_state_se(self):
    """Also use odometry at the end of air phase."""
    modulated_phase = torch.remainder(self._current_phase + 2 * torch.pi,
                                      2 * torch.pi)
    raw_contact = torch.where(
        torch.logical_and(modulated_phase > self._swing_cutoff,
                          modulated_phase < 2. * torch.pi), False, True)
    # print(f"Raw constact: {raw_contact}")
    self._first_stance_seen = torch.logical_or(self._first_stance_seen,
                                               raw_contact)
    return torch.where(self._first_stance_seen, raw_contact,
                       torch.ones_like(raw_contact))

  @property
  def normalized_phase(self):
    """Returns the leg's progress in the current state (swing or stance)."""
    modulated_phase = torch.remainder(self._current_phase + 2 * torch.pi,
                                      2 * torch.pi)
    return torch.where(modulated_phase < self._swing_cutoff,
                       modulated_phase / self._swing_cutoff,
                       (modulated_phase - self._swing_cutoff) /
                       (2 * torch.pi - self._swing_cutoff))

  @property
  def stance_duration(self):
    return (self._swing_cutoff) / (2 * torch.pi *
                                   self._stepping_frequency[:, None])

  @property
  def true_phase(self):
    return self._current_phase[:, 0] - self._initial_offset[:, 0]

  @property
  def cycle_progress(self):
    true_phase = torch.remainder(self.true_phase + 2 * torch.pi, 2 * torch.pi)
    return true_phase / (2 * torch.pi)

  @property
  def stepping_frequency(self):
    return self._stepping_frequency

  @stepping_frequency.setter
  def stepping_frequency(self, new_frequency: torch.Tensor):
    self._stepping_frequency = new_frequency

  @property
  def cycle_count(self):
    return self._cycle_count