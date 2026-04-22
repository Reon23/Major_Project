import math
import random
import threading
import time
from collections import defaultdict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import arp, ethernet, ipv4, packet
from ryu.ofproto import ofproto_v1_3


class PathBelief:
    SIGMA_OBS_MIN = 0.04
    SIGMA_OBS_MAX = 0.35

    def __init__(
        self,
        prior: float = 0.2,
        sigma_prior: float = 0.15,
        sigma_obs: float = 0.10,
        alpha: float = 0.3,
    ):
        self.prior = prior
        self.sigma_prior = sigma_prior
        self.sigma_obs = sigma_obs
        self._alpha = alpha
        self.mu = prior
        self._last_obs = prior

    # Perception

    def update(self, observation: float):
        self._last_obs = max(0.0, min(1.0, observation))
        self.mu = self.mu + self._alpha * (self._last_obs - self.mu)
        self.mu = max(0.0, min(1.0, self.mu))

    def reinforce_confidence(self, rate: float = 0.02):
        """Active path: observations reduce measurement uncertainty."""
        self.sigma_obs = max(self.SIGMA_OBS_MIN, self.sigma_obs - rate)

    def decay_confidence(self, rate: float = 0.05):
        """Idle path: lack of observations inflates uncertainty."""
        self.sigma_obs = min(self.SIGMA_OBS_MAX, self.sigma_obs + rate)

    @property
    def free_energy(self) -> float:
        pe = (self._last_obs - self.mu) ** 2 / (2 * self.sigma_obs**2)
        kl = (self.mu - self.prior) ** 2 / (2 * self.sigma_prior**2)
        return pe + kl

    def predict_after_load_added(self, delta: float) -> float:
        return min(1.0, self.mu + delta)

    def predict_after_load_removed(self, delta: float) -> float:
        return max(0.0, self.mu - delta)

    @property
    def utilisation(self) -> float:
        return self.mu

    def __repr__(self):
        return (
            f"PathBelief(mu={self.mu:.3f}, F={self.free_energy:.4f}, "
            f"sigma_obs={self.sigma_obs:.3f})"
        )


#  Controller


class ActiveInferenceLB(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # Host metadata
    HOST_IPS = {
        "h1": "10.0.0.1",
        "h2": "10.0.0.2",
        "h3": "10.0.0.3",
        "h4": "10.0.0.4",
        "h5": "10.0.0.5",
        "h6": "10.0.0.6",
    }
    IP_TO_HOST = {v: k for k, v in HOST_IPS.items()}

    # Which switch and which port a host is connected to
    HOST_SWITCH = {"h1": 1, "h3": 2, "h4": 2, "h5": 3, "h6": 3, "h2": 4}
    HOST_PORT = {"h1": 1, "h3": 1, "h4": 2, "h5": 1, "h6": 2, "h2": 1}

    S1_UPLINKS = [2, 3]
    S2_S1_PORT = 3
    S2_S4_PORT = 4
    S3_S1_PORT = 3
    S3_S4_PORT = 4
    S4_UPLINKS = [2, 3]
    S4_S2_PORT = 2
    S4_S3_PORT = 3

    PATH_BW_MBPS = {0: 10.0, 1: 10.0}

    POLL_INTERVAL = 2

    EFE_TEMPERATURE = 8.0

    LOAD_TRANSFER_FRACTION = 0.6

    SWITCH_PROB_THRESHOLD = 0.60

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._datapaths = {}
        self._ip_to_mac = {}
        self._mac_to_ip = {}
        self._static_installed = False
        self._lock = threading.Lock()

        self._active_path = 0

        self._beliefs = {0: PathBelief(), 1: PathBelief()}

        self._last_bytes = defaultdict(int)
        self._last_time = {}

        self._h1h2_load_estimate = 0.0

        self.monitor_thread = hub.spawn(self._monitor_loop)

    #  OpenFlow

    def add_flow(self, dp, priority, match, actions, idle_timeout=0, hard_timeout=0):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(
            parser.OFPFlowMod(
                datapath=dp,
                priority=priority,
                match=match,
                instructions=inst,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
            )
        )

    def del_flows(self, dp, match, priority=20):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dp.send_msg(
            parser.OFPFlowMod(
                datapath=dp,
                command=ofproto.OFPFC_DELETE,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                priority=priority,
                match=match,
            )
        )

    def pkt_out(self, dp, in_port, actions, data):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dp.send_msg(
            parser.OFPPacketOut(
                datapath=dp,
                buffer_id=ofproto.OFP_NO_BUFFER,
                in_port=in_port,
                actions=actions,
                data=data,
            )
        )

    #  Switch connect

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        with self._lock:
            self._datapaths[dp.id] = dp
        self.logger.info("Switch s%d connected", dp.id)
        self.add_flow(
            dp,
            0,
            parser.OFPMatch(),
            [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)],
        )

    #  Flow installation

    def _ready(self):
        return (
            len(self._datapaths) == 4
            and self.HOST_IPS["h1"] in self._ip_to_mac
            and self.HOST_IPS["h2"] in self._ip_to_mac
        )

    def install_all_flows(self):
        if self._static_installed:
            return
        if not self._ready():
            return
        self._static_installed = True
        self.logger.info(
            "*** Installing all flows (active path = %d) ***", self._active_path
        )
        self._install_local_flows()
        self._push_h1h2_flows(self._active_path)
        self._install_lateral_flows()
        self._install_cross_relay_flows()

    def _install_local_flows(self):
        pairs = [
            (2, [("h3", 1, "h4", 2), ("h4", 2, "h3", 1)]),
            (3, [("h5", 1, "h6", 2), ("h6", 2, "h5", 1)]),
        ]
        for dpid, routes in pairs:
            dp = self._datapaths.get(dpid)
            if not dp:
                continue
            p = dp.ofproto_parser
            for src_h, src_port, dst_h, dst_port in routes:
                self.add_flow(
                    dp,
                    20,
                    p.OFPMatch(
                        in_port=src_port,
                        eth_type=0x0800,
                        ipv4_src=self.HOST_IPS[src_h],
                        ipv4_dst=self.HOST_IPS[dst_h],
                    ),
                    [p.OFPActionOutput(dst_port)],
                )
            self.logger.info("s%d: local host flows installed", dpid)

    def _push_h1h2_flows(self, path_idx: int):
        h1ip = self.HOST_IPS["h1"]
        h2ip = self.HOST_IPS["h2"]

        s1 = self._datapaths.get(1)
        s2 = self._datapaths.get(2)
        s3 = self._datapaths.get(3)
        s4 = self._datapaths.get(4)

        if not all([s1, s2, s3, s4]):
            self.logger.warning("Cannot push h1<->h2 flows — not all DPs ready")
            return

        relay_dp = s2 if path_idx == 0 else s3
        relay_dpid = 2 if path_idx == 0 else 3
        relay_s1p = self.S2_S1_PORT
        relay_s4p = self.S2_S4_PORT
        s1_ul = self.S1_UPLINKS[path_idx]
        s4_ul = self.S4_UPLINKS[path_idx]

        p = s1.ofproto_parser
        self.del_flows(
            s1, p.OFPMatch(in_port=1, eth_type=0x0800, ipv4_src=h1ip, ipv4_dst=h2ip)
        )
        for ul in self.S1_UPLINKS:
            self.del_flows(
                s1,
                p.OFPMatch(in_port=ul, eth_type=0x0800, ipv4_src=h2ip, ipv4_dst=h1ip),
            )
        self.add_flow(
            s1,
            20,
            p.OFPMatch(in_port=1, eth_type=0x0800, ipv4_src=h1ip, ipv4_dst=h2ip),
            [p.OFPActionOutput(s1_ul)],
        )
        for ul in self.S1_UPLINKS:
            self.add_flow(
                s1,
                20,
                p.OFPMatch(in_port=ul, eth_type=0x0800, ipv4_src=h2ip, ipv4_dst=h1ip),
                [p.OFPActionOutput(1)],
            )
        self.logger.info("s1: h1->h2 via port%d installed", s1_ul)

        p = relay_dp.ofproto_parser
        self.del_flows(
            relay_dp, p.OFPMatch(in_port=relay_s1p, eth_type=0x0800, ipv4_dst=h2ip)
        )
        self.del_flows(
            relay_dp, p.OFPMatch(in_port=relay_s4p, eth_type=0x0800, ipv4_dst=h1ip)
        )
        self.add_flow(
            relay_dp,
            20,
            p.OFPMatch(in_port=relay_s1p, eth_type=0x0800, ipv4_dst=h2ip),
            [p.OFPActionOutput(relay_s4p)],
        )
        self.add_flow(
            relay_dp,
            20,
            p.OFPMatch(in_port=relay_s4p, eth_type=0x0800, ipv4_dst=h1ip),
            [p.OFPActionOutput(relay_s1p)],
        )
        self.logger.info("s%d: h1<->h2 relay flows installed", relay_dpid)

        p = s4.ofproto_parser
        for ul in self.S4_UPLINKS:
            self.del_flows(s4, p.OFPMatch(in_port=ul, eth_type=0x0800, ipv4_dst=h2ip))
        self.del_flows(s4, p.OFPMatch(in_port=1, eth_type=0x0800, ipv4_dst=h1ip))
        for ul in self.S4_UPLINKS:
            self.add_flow(
                s4,
                20,
                p.OFPMatch(in_port=ul, eth_type=0x0800, ipv4_dst=h2ip),
                [p.OFPActionOutput(1)],
            )
        self.add_flow(
            s4,
            20,
            p.OFPMatch(in_port=1, eth_type=0x0800, ipv4_dst=h1ip),
            [p.OFPActionOutput(s4_ul)],
        )
        self.logger.info("s4: h2->h1 via port%d installed", s4_ul)

    def _install_lateral_flows(self):
        h2ip = self.HOST_IPS["h2"]
        s2 = self._datapaths.get(2)
        s3 = self._datapaths.get(3)
        s4 = self._datapaths.get(4)

        if s2:
            p = s2.ofproto_parser
            for hname, hport in [("h3", 1), ("h4", 2)]:
                hip = self.HOST_IPS[hname]
                self.add_flow(
                    s2,
                    15,
                    p.OFPMatch(in_port=hport, eth_type=0x0800, ipv4_dst=h2ip),
                    [p.OFPActionOutput(self.S2_S4_PORT)],
                )
                self.add_flow(
                    s2,
                    15,
                    p.OFPMatch(in_port=self.S2_S4_PORT, eth_type=0x0800, ipv4_dst=hip),
                    [p.OFPActionOutput(hport)],
                )
            self.logger.info("s2: lateral h3/h4<->h2 flows installed")

        if s3:
            p = s3.ofproto_parser
            for hname, hport in [("h5", 1), ("h6", 2)]:
                hip = self.HOST_IPS[hname]
                self.add_flow(
                    s3,
                    15,
                    p.OFPMatch(in_port=hport, eth_type=0x0800, ipv4_dst=h2ip),
                    [p.OFPActionOutput(self.S3_S4_PORT)],
                )
                self.add_flow(
                    s3,
                    15,
                    p.OFPMatch(in_port=self.S3_S4_PORT, eth_type=0x0800, ipv4_dst=hip),
                    [p.OFPActionOutput(hport)],
                )
            self.logger.info("s3: lateral h5/h6<->h2 flows installed")

        if s4:
            p = s4.ofproto_parser
            for hname in ["h3", "h4"]:
                self.add_flow(
                    s4,
                    15,
                    p.OFPMatch(
                        in_port=1, eth_type=0x0800, ipv4_dst=self.HOST_IPS[hname]
                    ),
                    [p.OFPActionOutput(self.S4_S2_PORT)],
                )
            for hname in ["h5", "h6"]:
                self.add_flow(
                    s4,
                    15,
                    p.OFPMatch(
                        in_port=1, eth_type=0x0800, ipv4_dst=self.HOST_IPS[hname]
                    ),
                    [p.OFPActionOutput(self.S4_S3_PORT)],
                )
            self.logger.info("s4: lateral reverse flows installed")

    def _install_cross_relay_flows(self):
        s1 = self._datapaths.get(1)
        s2 = self._datapaths.get(2)
        s3 = self._datapaths.get(3)
        if not all([s1, s2, s3]):
            return

        s2_hosts = [("h3", 1), ("h4", 2)]
        s3_hosts = [("h5", 1), ("h6", 2)]

        p2 = s2.ofproto_parser
        for src_h, src_port in s2_hosts:
            for dst_h, _ in s3_hosts:
                self.add_flow(
                    s2,
                    10,
                    p2.OFPMatch(
                        in_port=src_port, eth_type=0x0800, ipv4_dst=self.HOST_IPS[dst_h]
                    ),
                    [p2.OFPActionOutput(self.S2_S1_PORT)],
                )
        for dst_h, dst_port in s2_hosts:
            self.add_flow(
                s2,
                10,
                p2.OFPMatch(
                    in_port=self.S2_S1_PORT,
                    eth_type=0x0800,
                    ipv4_dst=self.HOST_IPS[dst_h],
                ),
                [p2.OFPActionOutput(dst_port)],
            )
        self.logger.info("s2: cross-relay h3/h4<->h5/h6 flows installed")

        p1 = s1.ofproto_parser
        for dst_h, _ in s3_hosts:
            self.add_flow(
                s1,
                10,
                p1.OFPMatch(in_port=2, eth_type=0x0800, ipv4_dst=self.HOST_IPS[dst_h]),
                [p1.OFPActionOutput(3)],
            )
        for dst_h, _ in s2_hosts:
            self.add_flow(
                s1,
                10,
                p1.OFPMatch(in_port=3, eth_type=0x0800, ipv4_dst=self.HOST_IPS[dst_h]),
                [p1.OFPActionOutput(2)],
            )
        self.logger.info("s1: cross-relay transit flows installed")

        p3 = s3.ofproto_parser
        for src_h, src_port in s3_hosts:
            for dst_h, _ in s2_hosts:
                self.add_flow(
                    s3,
                    10,
                    p3.OFPMatch(
                        in_port=src_port, eth_type=0x0800, ipv4_dst=self.HOST_IPS[dst_h]
                    ),
                    [p3.OFPActionOutput(self.S3_S1_PORT)],
                )
        for dst_h, dst_port in s3_hosts:
            self.add_flow(
                s3,
                10,
                p3.OFPMatch(
                    in_port=self.S3_S1_PORT,
                    eth_type=0x0800,
                    ipv4_dst=self.HOST_IPS[dst_h],
                ),
                [p3.OFPActionOutput(dst_port)],
            )
        self.logger.info("s3: cross-relay h5/h6<->h3/h4 flows installed")

    def _monitor_loop(self):
        hub.sleep(4)
        while True:
            with self._lock:
                dps = list(self._datapaths.values())
            for dp in dps:
                self._request_port_stats(dp)
            hub.sleep(self.POLL_INTERVAL)

    def _request_port_stats(self, dp):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofproto.OFPP_ANY))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        """
        1. Compute per-port utilisation from s1 uplinks.
        2. Update PathBelief (perception) and confidence.
        3. Estimate h1<->h2 load contribution (transition model input).
        4. Compute Expected Free Energy for each policy (stay / switch).
        5. Select policy via softmax; reroute if switch wins above threshold.
        """
        dpid = ev.msg.datapath.id
        now = time.time()

        if dpid != 1:
            return

        port_util = {}

        for stat in ev.msg.body:
            port_no = stat.port_no
            if port_no not in (2, 3):
                continue

            key = (dpid, port_no)
            total_bytes = stat.rx_bytes + stat.tx_bytes
            last_b = self._last_bytes.get(key, total_bytes)
            last_t = self._last_time.get(key, now)
            dt = max(now - last_t, 0.001)

            delta_bytes = max(0, total_bytes - last_b)
            rate_mbps = (delta_bytes * 8) / 1e6 / dt
            path_idx = 0 if port_no == 2 else 1
            util = min(rate_mbps / self.PATH_BW_MBPS[path_idx], 1.0)

            self._last_bytes[key] = total_bytes
            self._last_time[key] = now
            port_util[path_idx] = util

            self.logger.info(
                "  [s1-eth%d] rx=%-10d tx=%-10d delta=%-8d  %.3f Mbps  util=%5.1f%%",
                port_no,
                stat.rx_bytes,
                stat.tx_bytes,
                delta_bytes,
                rate_mbps,
                util * 100,
            )

        if not port_util:
            return

        with self._lock:
            cur = self._active_path
            alt = 1 - cur

        for path_idx, util in port_util.items():
            self._beliefs[path_idx].update(util)

        self._beliefs[cur].reinforce_confidence()
        self._beliefs[alt].decay_confidence()

        cur_mu = self._beliefs[cur].mu
        alt_mu = self._beliefs[alt].mu
        h1h2_load = max(0.0, cur_mu - alt_mu)

        self._h1h2_load_estimate = 0.6 * self._h1h2_load_estimate + 0.4 * h1h2_load
        h1h2 = self._h1h2_load_estimate

        G = self._compute_efe(cur, alt, h1h2)

        g_stay, g_switch = G[0], G[1]

        g_min = min(g_stay, g_switch)
        exp_stay = math.exp(-self.EFE_TEMPERATURE * (g_stay - g_min))
        exp_switch = math.exp(-self.EFE_TEMPERATURE * (g_switch - g_min))
        denom = exp_stay + exp_switch
        p_switch = exp_switch / denom
        p_stay = exp_stay / denom

        self.logger.info(
            "\n┌─ Active Inference ──────────────────────────────────────────┐\n"
            "│  path 0  mu=%.3f  F=%.4f  sigma_obs=%.3f  util=%5.1f%%     \n"
            "│  path 1  mu=%.3f  F=%.4f  sigma_obs=%.3f  util=%5.1f%%     \n"
            "│  h1<->h2 load estimate: %.3f                                \n"
            "│  G(stay)=%.4f  G(switch)=%.4f                              \n"
            "│  P(stay)=%.3f  P(switch)=%.3f  active=path%d               \n"
            "└─────────────────────────────────────────────────────────────┘",
            self._beliefs[0].mu,
            self._beliefs[0].free_energy,
            self._beliefs[0].sigma_obs,
            self._beliefs[0].utilisation * 100,
            self._beliefs[1].mu,
            self._beliefs[1].free_energy,
            self._beliefs[1].sigma_obs,
            self._beliefs[1].utilisation * 100,
            h1h2,
            g_stay,
            g_switch,
            p_stay,
            p_switch,
            cur,
        )

        if not self._static_installed:
            return

        chosen_action = 1 if random.random() < p_switch else 0

        if chosen_action == 1 and p_switch >= self.SWITCH_PROB_THRESHOLD:
            self.logger.warning(
                "  *** REROUTING h1<->h2: path%d -> path%d "
                "(P(switch)=%.3f, G_stay=%.4f, G_switch=%.4f) ***",
                cur,
                alt,
                p_switch,
                g_stay,
                g_switch,
            )
            with self._lock:
                self._active_path = alt
            self._push_h1h2_flows(alt)

    def _compute_efe(self, cur: int, alt: int, h1h2_load: float) -> dict:
        b_cur = self._beliefs[cur]
        b_alt = self._beliefs[alt]

        pred_cur_stay = b_cur.mu  # current path keeps h1<->h2 traffic
        pred_alt_stay = b_alt.mu  # alt path is unchanged

        extrinsic_stay = (pred_cur_stay - b_cur.prior) ** 2 / (
            2 * b_cur.sigma_prior**2
        ) + (pred_alt_stay - b_alt.prior) ** 2 / (2 * b_alt.sigma_prior**2)

        epistemic_stay = 0.0
        G_stay = extrinsic_stay + epistemic_stay

        pred_cur_switch = b_cur.predict_after_load_removed(h1h2_load)
        pred_alt_switch = b_alt.predict_after_load_added(h1h2_load)

        extrinsic_switch = (pred_cur_switch - b_cur.prior) ** 2 / (
            2 * b_cur.sigma_prior**2
        ) + (pred_alt_switch - b_alt.prior) ** 2 / (2 * b_alt.sigma_prior**2)

        epistemic_switch = b_alt.sigma_obs
        G_switch = extrinsic_switch + epistemic_switch

        return {0: G_stay, 1: G_switch}

    #  Packet-in handler

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id
        in_port = msg.match["in_port"]

        with self._lock:
            self._datapaths[dpid] = dp

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            src_ip = arp_pkt.src_ip
            src_mac = arp_pkt.src_mac
            if src_ip in self.IP_TO_HOST and src_ip not in self._ip_to_mac:
                self._ip_to_mac[src_ip] = src_mac
                self._mac_to_ip[src_mac] = src_ip
                self.logger.info("Learned %s MAC: %s", self.IP_TO_HOST[src_ip], src_mac)
            self.install_all_flows()
            self.pkt_out(
                dp, in_port, [parser.OFPActionOutput(ofproto.OFPP_FLOOD)], msg.data
            )
            return

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt is None:
            return

        out_port = self._derive_out_port(dpid, in_port, ip_pkt.src, ip_pkt.dst)
        if out_port is not None:
            self.pkt_out(dp, in_port, [parser.OFPActionOutput(out_port)], msg.data)

    def _derive_out_port(self, dpid, in_port, src_ip, dst_ip):
        dst_host = self.IP_TO_HOST.get(dst_ip)
        if dst_host is None:
            return None

        dst_sw = self.HOST_SWITCH[dst_host]
        dst_port = self.HOST_PORT[dst_host]

        with self._lock:
            path = self._active_path

        if dpid == 1:  # s1
            if dst_sw == 1:
                return dst_port
            if dst_sw == 2:
                return self.S1_UPLINKS[0]
            if dst_sw == 3:
                return self.S1_UPLINKS[1]
            if dst_sw == 4:
                return self.S1_UPLINKS[path]

        elif dpid == 2:  # s2
            if dst_sw == 2:
                return dst_port
            if dst_sw == 1:
                return self.S2_S1_PORT
            if dst_sw == 3:
                return self.S2_S1_PORT
            if dst_sw == 4:
                return self.S2_S4_PORT

        elif dpid == 3:  # s3
            if dst_sw == 3:
                return dst_port
            if dst_sw == 1:
                return self.S3_S1_PORT
            if dst_sw == 2:
                return self.S3_S1_PORT
            if dst_sw == 4:
                return self.S3_S4_PORT

        elif dpid == 4:  # s4
            if dst_sw == 4:
                return dst_port
            if dst_sw == 2:
                return self.S4_S2_PORT
            if dst_sw == 3:
                return self.S4_S3_PORT
            if dst_sw == 1:
                return self.S4_UPLINKS[path]

        return None
