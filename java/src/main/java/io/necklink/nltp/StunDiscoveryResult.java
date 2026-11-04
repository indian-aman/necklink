package io.necklink.nltp;

import java.util.Collections;
import java.util.List;
import java.util.Locale;

/**
 * Result of an RFC 5389 WAN STUN reachability &amp; NAT cone/symmetric discovery probe.
 */
public final class StunDiscoveryResult {
    public final String publicIp;
    public final int publicPort;
    public final int localPort;
    public final double rttMs;
    public final boolean endpointIndependentNat;
    public final String natSummary;
    public final List<String> serversQueried;

    public StunDiscoveryResult(
            String publicIp,
            int publicPort,
            int localPort,
            double rttMs,
            boolean endpointIndependentNat,
            String natSummary,
            List<String> serversQueried
    ) {
        this.publicIp = publicIp;
        this.publicPort = publicPort;
        this.localPort = localPort;
        this.rttMs = rttMs;
        this.endpointIndependentNat = endpointIndependentNat;
        this.natSummary = natSummary;
        this.serversQueried = Collections.unmodifiableList(serversQueried);
    }

    public String toJson(boolean showIp) {
        String ipOut = showIp ? publicIp : "xxx.xxx.xxx.xxx";
        return String.format(
                Locale.US,
                "{\"public_ip\":\"%s\",\"public_port\":%d,\"local_port\":%d,\"rtt_ms\":%.2f,\"endpoint_independent_nat\":%s,\"nat_summary\":\"%s\"}",
                NLTPWire.jsonEscape(ipOut),
                publicPort,
                localPort,
                rttMs,
                endpointIndependentNat ? "true" : "false",
                NLTPWire.jsonEscape(natSummary)
        );
    }
}
