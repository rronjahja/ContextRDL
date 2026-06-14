import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_csv("results/scalability.csv")

plt.figure(figsize=(6.5, 4.2))

plt.plot(df["actions"], df["time_seconds"], marker="o", linewidth=1.8)

plt.xlabel("Number of concurrent action instances (|Act_t|)")
plt.ylabel("Resolver runtime (seconds)")
plt.title("Resolver runtime as a function of concurrent action instances")

plt.grid(True, linewidth=0.6, alpha=0.7)

plt.tight_layout()

plt.savefig("results/scalability_plot.png", dpi=300, bbox_inches="tight")

plt.show()