import random


def nondeterministic_schedule(actions):

    shuffled = actions.copy()
    random.shuffle(shuffled)

    return shuffled