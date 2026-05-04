# HPE ArubaOS-Switch OpenFlow Quirks

Gaps between this controller and HPE ArubaOS-Switch OpenFlow 1.3 implementation,
extracted from *HPE ArubaOS-Switch OpenFlow v1.3 Administrator Guide for 16.03*
(part number 5200-2934a, May 2017, edition 2).

## 1. Multi-Table Pipeline Is Mandatory — Controller Only Partially Handles It

**Impact: Partial — forwarding flows now use table 100, but the full pipeline is not managed.**

The controller detects HPE switches at connect time (via `OFPMP_DESC` vendor
classification in `SwitchRegistry`) and installs forwarding flows, table-miss,
and drop rules on **table 100** (the Policy Engine table) instead of table 0.
This avoids table 0 entirely — OVS and ZodiacFX still use table 0 since they
expose a flat single-table model.

HPE OpenFlow 1.3 exposes a **multi-table pipeline** whose structure depends on the
configured pipeline model:

| Pipeline Model   | Tables                               |
|:-----------------|:-------------------------------------|
| Standard Match   | 0 (Start) → 100 (Policy Engine) → 200 → 201 → 202 → 203 |
| IP Control       | 0 (Start) → 50 (IP Control) → 100 (Policy Engine) → 200 → 201 → 202 → 203 |
| Custom           | User-defined via `OFPMP_TABLE_FEATURES` |

The switch auto-populates table 0 at connect time (page 122):

- **Standard Match**: one rule — `GoTo table 100` (all wildcards).
- **IP Control**: three rules — `GoTo table 100` (all wildcards),
  `match eth_type=IPv4 → GoTo table 50`, `match eth_type=IPv6 → GoTo table 50`.

Since table 0 immediately forwards traffic to deeper tables, controller-installed
flows on table 0 are bypassed. The table-miss rule (`OUTPUT:CONTROLLER`) is
never reached.

```
     Switch auto-rules            Controller installs here
     ┌─────────────────────┐      ┌──────────────────────────┐
     │  Table 0 (Start)    │      │  Table 100 (Policy Eng)  │
     │  ┌────────────────┐ │      │                          │
     │  │ GoTo table 100 │─┼──────┼→ flows installed here    │
     │  └────────────────┘ │      │  table-miss: → CONTROLLER│
     └─────────────────────┘      └──────────────────────────┘
```

**Fix required**: Use the multi-table pipeline. Install forwarding flows on
table 100+ and modify table-miss rules on every intermediate table to `GoTo`
the next table.

---

## 2. Default Table-Miss Is DROP on Every Table

**Impact: No traffic traverses the pipeline without explicit table-miss configuration.**

> "OpenFlow 1.3 instance exposes a multi-table model. For every table, the
> action of the default table-miss rule is 'DROP'. The controller must
> appropriately modify the table-miss rule for every table, for traffic to
> traverse the multi-table pipeline." — page 103

The controller must install a table-miss rule (priority 0, all wildcards) on
every table in the pipeline with action `GoTo <next-table>`, and on the last
table with `OUTPUT:CONTROLLER`.

**Fix required**: After switch features are received, query the pipeline
(`OFPMP_TABLE_FEATURES`), then install per-table table-miss rules:

| Table | Table-Miss Action    |
|:------|:---------------------|
| 0     | GoTo 100 (already auto-installed by switch) |
| 100   | GoTo 200             |
| 200   | GoTo 201             |
| 201   | GoTo 202             |
| 202   | GoTo 203             |
| 203   | OUTPUT:CONTROLLER    |

---

## 3. Table 0 Is Described as Read-Only

**Impact: Flow-mod on table 0 may be rejected or silently ignored.**

> "Table 0, a read-only table, in the OpenFlow 1.3 multiple pipeline represents
> the start of the pipeline." — page 102

In Standard Match and IP Control modes the switch auto-manages table 0.
From firmware K/KA.15.16 onward, the switch does respond to flow-mod on table 0,
but deleting the auto-added `GoTo` rules causes the switch to drop all traffic
until the controller re-adds them (page 122). Earlier firmware may reject
table 0 flow-mod outright.

**Fix required**: Do not install forwarding flows on table 0. Do not delete the
switch's auto-added `GoTo` rules from table 0.

---

## 4. Idle Timeout vs. Hardware Statistics Refresh Rate

**Impact: Flows with `idle_timeout < 20 s` are deleted prematurely.**

HPE hardware stats refresh defaults to **20 seconds** (configurable via
`openflow hardware-statistics refresh-rate policy-engine-table <seconds>`).
Packet counters are not updated between refreshes, so the switch sees zero
packets and expires the flow (page 118).

Table 50 (IP Control) has a fixed **12-second** refresh rate, so flows there
need `idle_timeout ≥ 24 s` (page 103).

The controller's default `idle_timeout = 30 s` is above the 20 s threshold.
However, the table-miss rule uses `idle_timeout = 0` (permanent), which is safe.

**Fix required**: Enforce a minimum `idle_timeout ≥ 24 s` for HPE switches.
If the hardware refresh rate can be queried, set `idle_timeout ≥ 2 × refresh_rate`.

---

## 5. Unsupported Special Ports

**Impact: `OFPP_ALL`, `OFPP_LOCAL`, `OFPP_TABLE`, `OFPP_IN_PORT` are rejected.**

> "Special ports OFPP_ALL, OFPP_LOCAL, OFPP_TABLE, and OFPP_IN_PORT are not
> supported." — page 123

The controller only uses `OFPP_CONTROLLER` and physical port numbers, so this
has no direct impact on the current code. However, any future use of these
virtual ports would break.

---

## 6. Write-Instruction Restrictions

**Impact: Rules combining `OFPP_CONTROLLER` with `GoTo` are rejected.**

> "Write-instruction having OFPP_CONTROLLER cannot have a Goto instruction
> associated with it." — page 123

> "Rules with actions as packet-modifications and OFPP_CONTROLLER are directly
> rejected." — page 123

The controller currently uses only `OFPIT_APPLY_ACTIONS` with `OFPActionOutput`,
which is safe. When the controller is adapted to use multi-table `GoTo`
instructions, the table-miss rule on the last table must not combine `GoTo`
with `OUTPUT:CONTROLLER` — use only `OUTPUT:CONTROLLER`.

---

## 7. `write-metadata` Not Supported

> "Write-metadata instructions are not supported." — page 123

**No impact on current code** (not used), but precludes metadata-based
pipeline designs.

---

## 8. VLAN Tag Behavior Differs by Instance Mode

**Impact: Packet-in contents vary and may confuse host learning.**

| Mode            | VLAN in packet_in                                      |
|:----------------|:-------------------------------------------------------|
| Virtualization  | Tags are **stripped** from packet_in (page 121)        |
| Aggregation     | Tags are **always added** to packet_in, even for untagged ingress (page 121) |

The controller is VLAN-agnostic (no VLAN match fields in any `OFPMatch`). In
Virtualization mode this is fine. In Aggregation mode, the controller would see
spurious VLAN tags that it currently ignores.

**Fix required**: Account for per-mode VLAN tag presence when parsing packet_in.

---

## 9. `miss_send_len` Not Honored

> "The switch implementation does not honor the miss_send_len field specified
> in packet-in switch configuration messages. This occurs because the switch
> does not buffer packets, and the controller sees the entire packet copied in
> packet-in message with buffer_id set as OFP_NO_BUFFER." — page 121

**No impact on current code** (the controller already sets `OFPCML_NO_BUFFER`
and handles full packets), but `buffer_id` will always be `OFP_NO_BUFFER`.

---

## 10. Meter `prec_level` Replaces DSCP Instead of Incrementing

> "As per the OpenFlow specification 1.3.1, the prec_level given in the
> ofp_meter_band_dscp_remark indicates by what amount the DSCP value in the
> packets must be incremented if the packets exceed the band. However, the
> switch implementation directly replaces the DSCP value in the IP packets
> with the prec_level when the band exceeds the meter defined by the
> controller." — page 121

**No impact on current code** (meters not used). If QoS metering is added,
set `prec_level` to the absolute target DSCP, not the delta.

---

## 11. Same Meter in Pipeline Causes Skewed Rates

> "When a same OpenFlow meter is used two or more times in an OpenFlow
> pipeline, it results in skewed meter rates leading to unpredictable behavior
> in how the packets are metered. HPE recommends that you do not use a meter
> more than once in a packet pipeline." — page 123

**No impact on current code** (meters not used). If metering is added, each
meter ID should appear at most once per pipeline.

---

## 12. Controller Flows Processed in Software (Not Line-Rate)

> "Flows with an action to send matching traffic to controller are installed on
> hardware. But, the actual traffic forwarding takes place in software, since
> we must add the required OpenFlow specific headers." — page 118

**Impact: Performance.** Packets forwarded to `OFPP_CONTROLLER` are
processed by the CPU, not the ASIC. Table-miss traffic (all unknown packets in
the current design) will be rate-limited by the CPU rather than forwarded at
line rate.

The `limit software-rate` command (default 100 pps, page 121) caps the number
of packets sent to the controller per second.

**Fix required**: The reactive forwarding model (first packet triggers flow
install) still works, but the per-second packet-in rate is capped.

---

## 13. ARP Required for Traffic Destined to Switch MAC

> "When using OpenFlow, traffic that is destined to a routing switch that
> matches an OpenFlow flow that emulates routing does not get routed if there
> are no ARP entries on the switch for the devices involved. To make this work
> with OpenFlow, you must ensure that ARPs to all hosts are resolved." — page 122

The controller's proxy ARP handler mitigates this, but only if ARP requests
reach the controller via packet-in. If a host ARP entry expires and the
packet-in mechanism is not triggered (e.g., because the flow is already
installed in hardware), traffic may be dropped.

---

## 14. Instance Must Have Member VLAN and Controller VLAN

**Impact: OpenFlow instance will not come UP without explicit VLAN configuration.**

> "A controller and a member VLAN must be added to the named instance before
> enabling it." — page 98

> "Oper. Status is down when either the member VLAN of the OpenFlow instance
> does not exist on the switch or the controller VLAN does not exist." — page 119

Unlike OVS where a bridge is ready as soon as the controller connects, HPE
switches require pre-configured VLANs. The controller has no way to detect or
remedy this — the switch simply never sends a Features Reply with `ports`
populated.

---

## 15. VLAN Constraints

| Constraint                                          | Page |
|:----------------------------------------------------|:-----|
| Only one VLAN per instance (non-Multi-VLAN mode)    | 98   |
| Management VLAN cannot be member of an OF instance  | 98   |
| Controller interface VLAN cannot be member VLAN     | 98   |
| Dynamic VLAN cannot be a member VLAN                | 98   |
| VLAN must already exist on switch before membership | 98   |
| At least one member VLAN must be configured on switch or operStatus is DOWN | 123 |

---

## 16. Groups Limitations

| Restriction                                                      | Page   |
|:-----------------------------------------------------------------|:-------|
| Groups not supported in hardware in v1-compatible mode           | 123    |
| Special ports not supported in groups                            | 123    |
| Rules in hardware tables with group action must not have a Goto  | 123-124 |
| Rules in hardware tables cannot reference groups in software     | 123    |
| Maximum 1024 groups per instance                                 | 124    |
| Group modification that leaves zero ports is rejected            | 124    |

**No impact on current code** (groups not used).

---

## 17. Multi-VLAN Instance Constraints

| Constraint                                                                   | Page |
|:-----------------------------------------------------------------------------|:-----|
| First VLAN in Multi-VLAN is part of DPID (uniqueness guarantee)              | 123  |
| VLAN cannot be dynamically added to an enabled instance                      | 123  |
| At least one member VLAN must be configured on switch or operStatus is DOWN  | 123  |
| Management and controller VLANs cannot be part of instance                   | 123  |

---

## 18. Custom Pipeline Constraints

| Constraint                                                                   | Page |
|:-----------------------------------------------------------------------------|:-----|
| Only one custom pipeline instance is operationally UP by default             | 63   |
| Table modifications cannot be done on v1/v2 modules or v3 in compatible mode | 123  |
| Tables must have minimum 512 entries                                         | 123  |
| Before deleting a table, all flows pointing to it must be removed            | 123  |
| In Standard/IP-Control mode, the default pipeline cannot be changed          | 123  |
| `OFPP_ALL`, `OFPP_LOCAL`, `OFPP_TABLE`, `OFPP_IN_PORT` not supported        | 123  |
| IPv6: MAC SA modification and L4 port modification not allowed               | 123  |
| Counters are associated with tables/flow entries *if available*              | 123  |

---

## 19. OXM Match Field Limitations

Not all standard OXM fields are supported or maskable. See pages 114-116 for
the full matrix. Notable omissions:

- `OFPXMT_OFB_SCTP_SRC/DST` — not supported
- `OFPXMT_OFB_IPV6_ND_SLL/TLL` — not supported
- `OFPXMT_OFB_MPLS_*` — not supported
- `OFPXMT_OFB_PBB_ISID` — not supported
- `OFPXMT_OFB_TUNNEL_ID` — not supported
- `OFPXMT_OFB_IPV6_EXTHDR` — not supported
- `OFPXMT_OFB_IP_ECN` — not supported
- `OFPXMT_OFB_METADATA` — match not supported, mask not supported
- `OFPXMT_OFB_VLAN_VID` — match supported but **mask not supported**

**No impact on current code** (only `eth_dst`, `eth_src`, `in_port`, and
`eth_type` are used for matching).

---

## 20. HPE-Specific Experimenter Fields

HPE vendor ID is `0x00002481`. The switch uses experimenter OXM fields for:

| Feature               | OXM Field Values                        |
|:----------------------|:----------------------------------------|
| TCP flags matching    | oxm_field = 4                           |
| L4 port range matching| oxm_field = 0 (UDP src), 1 (UDP dst), 2 (TCP src), 3 (TCP dst) |
| Custom match fields   | oxm_field = 5-8 (CUSTOM_MATCH_ONE through FOUR) |

Controllers that negotiate to `1.3` must handle `OFPET_EXPERIMENTER` error
messages with this vendor ID in the experimenter field.

---

## 21. Push/Pop VLAN Restrictions

> "OFPAT_PUSH_VLAN and OFPAT_POP_VLAN are supported for single tagged packets
> only." — page 76

- `OFPAT_PUSH_VLAN` only with ethertype `0x8100` (802.1q).
- `OFPAT_PUSH_VLAN` on already-tagged packets **modifies** the existing tag
  instead of pushing a new one.
- `OFPAT_PUSH_VLAN` and `OFPAT_SET_FIELD(OFP_VLAN_VID)` cannot be combined in
  the same flow.

---

## 22. Port Modification Limitations

The following port config flags are **not supported** (page 13):

- `OFPPC_NO_STP`
- `OFPPC_NO_RECV`
- `OFPPC_NO_RECV_STP`
- `OFPPC_NO_FWD`

Sending them returns `OFPET_PORT_MOD_FAILED`.

---

## Summary of Required Changes

| Priority | Change                                                           |
|:---------|:-----------------------------------------------------------------|
| **P0**   | ~~Detect HPE switches~~ — done via `OFPMP_DESC` + `SwitchRegistry._classify()` |
| **P0**   | ~~Move forwarding flows from table 0 to table 100+~~ — done via `_main_table()` |
| **P0**   | Query `OFPMP_TABLE_FEATURES` to discover the full pipeline        |
| **P0**   | Install per-table table-miss rules (GoTo chain for tables beyond 100, OUTPUT:CONTROLLER on last) |
| **P0**   | Never delete table 0 auto-added GoTo rules (currently respected: baseline rules only touch table 100) |
| **P1**   | Enforce minimum `idle_timeout ≥ 24 s` on HPE switches            |
| **P1**   | Enforce minimum `idle_timeout ≥ 24 s` on HPE switches            |
| **P2**   | Handle VLAN tag presence/absence per instance mode               |
| **P2**   | Do not use `OFPP_ALL/LOCAL/TABLE/IN_PORT`                        |
| **P2**   | Do not combine `OFPP_CONTROLLER` with `GoTo` in write-instructions |
| **P2**   | Do not combine packet-modifications with `OFPP_CONTROLLER`       |
| **P3**   | If using meters: `prec_level` = absolute DSCP, not delta         |
| **P3**   | If using meters: each meter ID at most once per pipeline         |
| **P3**   | Handle `OFPET_EXPERIMENTER` errors with HPE vendor ID `0x00002481` |

---

## Confirmed behaviours (Aruba 2930F, firmware WC.16.07.0003)

| Flow-Mod | Field | Table 100 Result |
|:---------|:------|:-----------------|
| table-miss (all wildcards) → CONTROLLER | — | Accepted |
| drop IPv6 (eth_type=0x86DD, no mask) | eth_type | Accepted |
| drop IPv4 MC (eth_dst=01:00:5e/9 with mask) | eth_dst | **Rejected** — `OFPBMC_BAD_MASK(8)` |
