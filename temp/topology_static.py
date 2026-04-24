from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel


class DQNBottleneckTopo(Topo):
    def build(self):
        # Hosts
        h1 = self.addHost("h1", ip="10.0.0.1/24")
        h2 = self.addHost("h2", ip="10.0.0.2/24")
        h3 = self.addHost("h3", ip="10.0.0.3/24")
        h4 = self.addHost("h4", ip="10.0.0.4/24")
        h5 = self.addHost("h5", ip="10.0.0.5/24")
        h6 = self.addHost("h6", ip="10.0.0.6/24")

        # Switches
        s1 = self.addSwitch("s1", protocols="OpenFlow13")
        s2 = self.addSwitch("s2", protocols="OpenFlow13")
        s3 = self.addSwitch("s3", protocols="OpenFlow13")
        s4 = self.addSwitch("s4", protocols="OpenFlow13")

        # Host-to-switch links (50 Mbps, 1 ms)
        # s1: eth1=h1, eth2=h2
        self.addLink(h1, s1, cls=TCLink, bw=50, delay="1ms")
        self.addLink(h2, s4, cls=TCLink, bw=50, delay="1ms")
        # self.addLink(h2, s1, cls=TCLink, bw=50, delay="1ms")
        # s2: eth2=h3, eth3=h4
        self.addLink(h3, s2, cls=TCLink, bw=50, delay="1ms")
        self.addLink(h4, s2, cls=TCLink, bw=50, delay="1ms")
        # s3: eth2=h5, eth3=h6
        self.addLink(h5, s3, cls=TCLink, bw=50, delay="1ms")
        self.addLink(h6, s3, cls=TCLink, bw=50, delay="1ms")

        # Bottleneck inter-switch links (10 Mbps, 10 ms)
        # s1-eth3 ↔ s2-eth1
        self.addLink(
            s1,
            s2,
            cls=TCLink,
            bw=10,
            delay="10ms",
            max_queue_size=50,
            use_htb=True,
        )
        # s1-eth4 ↔ s3-eth1
        self.addLink(
            s1,
            s3,
            cls=TCLink,
            bw=10,
            delay="10ms",
            max_queue_size=50,
            use_htb=True,
        )
        # s2-eth4 ↔ s3-eth4  (detour link — enables actions 1 and 2)
        self.addLink(
            s2,
            s4,
            cls=TCLink,
            bw=10,
            delay="10ms",
            max_queue_size=50,
            use_htb=True,
        )
        self.addLink(
            s3,
            s4,
            cls=TCLink,
            bw=10,
            delay="10ms",
            max_queue_size=50,
            use_htb=True,
        )


def run():
    topo = DQNBottleneckTopo()
    net = Mininet(
        topo=topo,
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
    )
    net.addController(
        "c0",
        controller=RemoteController,
        ip="127.0.0.1",
        port=6633,
    )
    net.start()

    CLI(net)
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    run()
