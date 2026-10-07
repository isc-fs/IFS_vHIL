*** Comments ***
ECU smoke suite on a virtual MainLite (STM32H733).

Boots the unmodified ECU application image (the same ELF IFS_HIL flashes to a
real MainLite) and checks it from the CAN side only, as the physical bench's
Block A / Block I do. The ECU boots as the car's does, through the CAN
bootloader in sector 0, whose 2 s auto-jump window comes first: the first
frame of each case waits that much longer. The Renode script is generated
from systems/ecu.yaml, with the flash image it provisions:
    python -m vhil.system render systems/ecu.yaml --firmware ecu=/path/to/ECU08.elf \
        --firmware ecu.bootloader=/path/to/CAN_BL.elf --can-hub -o /tmp/ecu.resc
    renode-test tests/ecu_smoke.robot --variable ELF:/path/to/ECU08.elf --variable RESC:/tmp/ecu.resc


*** Variables ***
${ELF}                  ${EMPTY}
${RESC}                 ${EMPTY}

# can_map.py IDs (IFS_HIL tools/firmware_test/vcu/can_map.py)
${ID_HEARTBEAT}         0x100
${ID_PIT_HEALTH}        0x704
${ID_DASH_FIRST}        0x510
${ID_INV_CMD}           0x362

# 0x704 byte4 task-liveness bits (ECU >= 1.0.0, pit_diag_health.def)
${TASK_CONTROL}         0x01
${TASK_CAN_RX}          0x02
${TASK_CAN_TX}          0x04
${TASK_TELEMETRY}       0x08
${TASK_DIAG}            0x10


*** Keywords ***
Boot ECU
    [Arguments]    ${hub}
    Should Not Be Empty    ${ELF}    Pass the image with --variable ELF:/path/to/ECU08.elf
    Should Not Be Empty    ${RESC}    Pass the script rendered from systems/ecu.yaml (with --firmware) as --variable RESC:<path>
    Execute Command    $elf_ecu=@${ELF}
    Execute Command    include @${RESC}
    # 3 s, plus the bootloader's 2 s window before the app's first frame.
    Create CAN Tester    ${hub}    defaultTimeout=5

Count Frames With Id
    [Arguments]    ${id}    ${count}
    FOR    ${i}    IN RANGE    ${count}
        Wait For Frame With Id    ${id}
    END

Health Byte
    [Arguments]    ${index}
    ${data}=    Wait For Frame With Id    ${ID_PIT_HEALTH}    timeout=4
    ${byte}=    Evaluate    $data[${index}]
    RETURN    ${byte}


*** Test Cases ***
Should Send The VCU Heartbeat On The ACU Bus
    [Documentation]    0x100 is what the AMS watches to know the VCU is alive (A-block).
    Boot ECU    can_acu
    Count Frames With Id    ${ID_HEARTBEAT}    10

Should Command The Inverter On The INV Bus
    Boot ECU    can_inv
    Count Frames With Id    ${ID_INV_CMD}    10

Should Drive The Dash Bus On FDCAN3
    [Documentation]    FDCAN3 exists only on the H72x/H73x; this proves the custom platform.
    Boot ECU    can_dash
    Count Frames With Id    ${ID_DASH_FIRST}    3

Should Publish The Health Frame At 1 Hz
    [Documentation]    0x704 is ungated, from DiagTask (I-001).
    Boot ECU    can_acu
    Wait For Frame With Id    ${ID_PIT_HEALTH}    timeout=4
    Wait For Frame With Id    ${ID_PIT_HEALTH}    timeout=1.2

Health Frame Should Show All Five Tasks Running
    [Documentation]    I-003. CanRxTask bumps its liveness on every 100 ms queue timeout
    ...    (can_rx_task.cpp, CanRxWaitMs), so all five bits are set on a quiet bus too.
    Boot ECU    can_acu
    ${seen}=    Set Variable    ${0}
    FOR    ${i}    IN RANGE    4
        ${b4}=    Health Byte    4
        ${seen}=    Evaluate    ${seen} | ${b4}
    END
    ${want}=    Evaluate    ${TASK_CONTROL} | ${TASK_CAN_RX} | ${TASK_CAN_TX} | ${TASK_TELEMETRY} | ${TASK_DIAG}
    ${missing}=    Evaluate    ${want} & ~${seen}
    Should Be Equal As Integers    ${missing}    0    task bits missing: ${missing}

Health Frame Should Report A Known Reset Cause
    [Documentation]    I-002: byte5 b0-b2, 0..6 (ResetCause).
    Boot ECU    can_acu
    ${b5}=    Health Byte    5
    ${cause}=    Evaluate    ${b5} & 0x07
    Should Be True    ${cause} <= 6    reset_cause ${cause} is not a ResetCause
