"""
topology.py — Sample Mininet Topology for Active Inference SDN Controller
=========================================================================

Topology (can be changed freely — controller is topology-independent):

         h1          h3  h4
          \          |  |
           s1 ──── s2
           |          \
           |           s4 ── h2
           |          /
           s1 ──── s3
          /     |    |
         h5    h6

Switches: s1, s2, s3, s4 (OpenFlow 1.3)
Hosts:    h1..h6

Inter-switch links (bottleneck):
  s1 <--> s2   10 Mbps, 10ms, queue 50
  s1 <--> s3   10 Mbps, 10ms, queue 50
  s2 <--> s4   10 Mbps, 10ms, queue 50
  s3 <--> s4   10 Mbps, 10ms, queue 50

Host-to-switch links (access):
  h1 <--> s1   50 Mbps, 1ms
  h2 <--> s4   50 Mbps, 1ms
  h3 <--> s2   50 Mbps, 1ms
  h4 <--> s2   50 Mbps, 1ms
  h5 <--> s3   50 Mbps, 1ms
  h6 <--> s3   50 Mbps, 1ms

Controller: RemoteController at 127.0.0.1:6633

Run:
    sudo python3 topology.py
"""

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel


class DynamicTopo(Topo):
    """
    Topology with 6 hosts, 4 switches, and multiple redundant paths.
    Designed to stress-test the Active Inference routing controller.

    Redundant paths from h1 (on s1) to h2 (on s4):
        Path A: s1 -> s2 -> s4
        Path B: s1 -> s3 -> s4
    """

    def build(self):
        # ── Hosts ─────────────────────────────────────────────────────────────
        h1 = self.addHost("h1", ip="10.0.0.1/24")
        h2 = self.addHost("h2", ip="10.0.0.2/24")
        h3 = self.addHost("h3", ip="10.0.0.3/24")
        h4 = self.addHost("h4", ip="10.0.0.4/24")
        h5 = self.addHost("h5", ip="10.0.0.5/24")
        h6 = self.addHost("h6", ip="10.0.0.6/24")

        # ── Switches (OpenFlow 1.3) ────────────────────────────────────────────
        s1 = self.addSwitch("s1", protocols="OpenFlow13")
        s2 = self.addSwitch("s2", protocols="OpenFlow13")
        s3 = self.addSwitch("s3", protocols="OpenFlow13")
        s4 = self.addSwitch("s4", protocols="OpenFlow13")

        # ── Host-to-switch links (50 Mbps access links, 1 ms delay) ───────────
        self.addLink(h1, s1, cls=TCLink, bw=50, delay="1ms")
        self.addLink(h2, s4, cls=TCLink, bw=50, delay="1ms")
        self.addLink(h3, s2, cls=TCLink, bw=50, delay="1ms")
        self.addLink(h4, s2, cls=TCLink, bw=50, delay="1ms")
        self.addLink(h5, s3, cls=TCLink, bw=50, delay="1ms")
        self.addLink(h6, s3, cls=TCLink, bw=50, delay="1ms")

        # ── Inter-switch bottleneck links (10 Mbps, 10 ms, queue 50) ──────────
        # s1 -- s2  (path A segment 1)
        self.addLink(
            s1, s2,
            cls=TCLink,
            bw=10,
            delay="10ms",
            max_queue_size=50,
            use_htb=True,
        )
        # s1 -- s3  (path B segment 1)
        self.addLink(
            s1, s3,
            cls=TCLink,
            bw=10,
            delay="10ms",
            max_queue_size=50,
            use_htb=True,
        )
        # s2 -- s4  (path A segment 2)
        self.addLink(
            s2, s4,
            cls=TCLink,
            bw=10,
            delay="10ms",
            max_queue_size=50,
            use_htb=True,
        )
        # s3 -- s4  (path B segment 2)
        self.addLink(
            s3, s4,
            cls=TCLink,
            bw=10,
            delay="10ms",
            max_queue_size=50,
            use_htb=True,
        )


def run():
    topo = DynamicTopo()
    net = Mininet(
        topo=topo,
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
    )

    # Connect to the Ryu remote controller
    net.addController(
        "c0",
        controller=RemoteController,
        ip="127.0.0.1",
        port=6633,
    )

    net.start()
    print("\n*** Mininet topology started")
    print("*** Hosts:", [h.name for h in net.hosts])
    print("*** Switches:", [s.name for s in net.switches])
    print("\n*** Test connectivity:")
    net.pingAll()
    print("\n*** Entering CLI — type 'exit' to quit")
    CLI(net)
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    run()
