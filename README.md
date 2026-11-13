# Netlea Bluetooth Filter for Home Assistant

Unofficial Home Assistant custom integration for Netlea Bluetooth filters that advertise as `NL NO.2S-G1`.

This integration communicates with the filter locally over Bluetooth Low Energy. It is not affiliated with or endorsed by Netlea. The BLE protocol was implemented from observed device behavior and has not been confirmed by the manufacturer.

## Features

- Filter status and availability
- Pump control and gear selection
- Daily schedule controls
- Feeding pause and resume
- Bluetooth connection release/recovery switch

Available entities depend on the device and Home Assistant version. The integration also registers services under the `netlea_filter` domain; their descriptions are in [`services.yaml`](custom_components/netlea_filter/services.yaml).

## Requirements

- Home Assistant with its Bluetooth integration configured
- A Bluetooth adapter or Bluetooth proxy that can reach the filter
- A compatible Netlea Bluetooth filter

## Manual installation

1. Copy `custom_components/netlea_filter` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration** and search for **Netlea Bluetooth Filter**. If Home Assistant discovers the filter, select its discovery notification instead.
4. Follow the setup form and enter the Bluetooth advertisement name shown by the device (commonly `NL NO.2S-G1`).

The integration does not require a fixed Bluetooth MAC address. It looks up the device through Home Assistant's Bluetooth discovery data.

## HACS

This integration is not listed in the default HACS store. Add `https://github.com/zuoVision/netlea` in HACS as a **custom repository** with category **Integration**. The repository uses the `custom_components/netlea_filter/` directory structure.

## Safety and support

Bluetooth control may fail or become unavailable depending on the adapter, Bluetooth proxy, Home Assistant host, or filter firmware. Keep the filter's physical controls available. Check the filter directly after any failed control request. Do not rely on this integration as a safety mechanism for aquarium life support.

When reporting an issue, include the Home Assistant version, filter model/firmware if known, relevant integration logs, and steps to reproduce. Remove Bluetooth addresses and other personal information from logs before posting them publicly.

## License

MIT. See [`LICENSE`](LICENSE).
