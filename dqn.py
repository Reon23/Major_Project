import torch
from torch import nn
import torch.nn.functional as F


class DQN(nn.Module):

    # Define layers
    def __init__(self, state_dim, action_dim, hidden_dim=256):
        super(DQN, self).__init__()

        # NOTE: Input layer implicitly defined

        # Hidden layer
        self.fc1 = nn.Linear(state_dim, hidden_dim)
        # Action layer
        self.fc2 = nn.Linear(hidden_dim, action_dim)

    # Performs calculations
    def forward(self, x):
        x = F.relu(self.fc1(x))
        return self.fc2(x)


if __name__ == "__main__":
    state_dim = 12
    action_dim = 2
    net = DQN(state_dim, action_dim)
    state = torch.randn(1, state_dim)
    output = net(state)
    print(output)
