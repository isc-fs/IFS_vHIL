*** Comments ***
AMS smoke suite on systems/ams.yaml: the unmodified AMS application on a
virtual MainLite, with its battery as models (LTC6820 bridge + 10 LTC6811s on
isoSPI). Stimulus goes through the models' own API and checks read the AMS's
CAN output, so the AMS is judged on what it reports about the battery it is
given. Run with:
    python -m vhil.system render systems/ams.yaml -o /tmp/ams.resc
    renode-test tests/ams_smoke.robot --variable ELF:/path/to/AMS.elf --variable RESC:/tmp/ams.resc


*** Variables ***
${ELF}                  ${EMPTY}
${RESC}                 ${EMPTY}
${CHAIN}                sysbus.spi1.isospi

# AMS CAN contract (IFS08-CE-AMS Core/Inc/can/messages)
${ID_STATUS}            0x4A0    # fsm_state, ams_ok, module_online_mask, min/max cell mV (BE)
${ID_TEMPS}             0x4A2    # min/max/avg temp, int8 degC
${ALL_MODULES}          0x1F


*** Keywords ***
Boot AMS
    Should Not Be Empty    ${ELF}    Pass the image with --variable ELF:/path/to/AMS.elf
    Should Not Be Empty    ${RESC}    Pass the script rendered from systems/ams.yaml with --variable RESC:<path>
    Execute Command    $elf_ams=@${ELF}
    Execute Command    include @${RESC}
    Create CAN Tester    can_acu    defaultTimeout=3

Status Should Become
    [Documentation]    Wait (up to ${frames} status frames, 500 ms apart) for a 0x4A0 field to equal a value.
    [Arguments]    ${field}    ${expected}    ${frames}=10
    FOR    ${i}    IN RANGE    ${frames}
        ${d}=    Wait For Frame With Id    ${ID_STATUS}
        ${got}=    Evaluate    {"online": $d[2], "min_mV": ($d[4] << 8) | $d[5], "max_mV": ($d[6] << 8) | $d[7]}["${field}"]
        IF    ${got} == ${expected}    RETURN
    END
    Fail    0x4A0 ${field} never became ${expected} (last ${got})

Temps Should Become
    [Arguments]    ${min}    ${max}    ${avg}    ${frames}=10
    FOR    ${i}    IN RANGE    ${frames}
        ${d}=    Wait For Frame With Id    ${ID_TEMPS}
        ${got}=    Evaluate    [b - 256 if b > 127 else b for b in list($d[0:3])]
        IF    ${got} == [${min}, ${max}, ${avg}]    RETURN
    END
    Fail    0x4A2 min/max/avg never became ${min}/${max}/${avg} (last ${got})


*** Test Cases ***
Should Discover The Chain And See Every Module
    Boot AMS
    Status Should Become    online    ${ALL_MODULES}
    Status Should Become    min_mV    3700
    Status Should Become    max_mV    3700

Should Report The Cell Voltages The Battery Has
    Boot AMS
    Status Should Become    online    ${ALL_MODULES}
    Execute Command    ${CHAIN} SetAllCells 3650
    Status Should Become    min_mV    3650
    Status Should Become    max_mV    3650

Should Find One Low Cell Deep In The Chain
    [Documentation]    cells7 = module 3 lower LTC; its C3 is module cell 11.
    Boot AMS
    Status Should Become    online    ${ALL_MODULES}
    Execute Command    ${CHAIN}.cells7 SetCell 2 3480
    Status Should Become    min_mV    3480
    Status Should Become    max_mV    3700

Should Report The Temperatures The Battery Has
    Boot AMS
    Status Should Become    online    ${ALL_MODULES}
    Temps Should Become    25    25    25
    Execute Command    ${CHAIN} SetAllTemperatures 320
    Temps Should Become    32    32    32    frames=12

Should Take A Module Offline When One Of Its Chips Goes Silent
    [Documentation]    cells0 is module 0's upper LTC: module 0 (bit 0) drops out.
    Boot AMS
    Status Should Become    online    ${ALL_MODULES}
    Execute Command    ${CHAIN}.cells0 Respond false
    Status Should Become    online    0x1E

Should Lose Everything Beyond A Cut isoSPI Link
    [Documentation]    Cut after cells5 (module 2 lower): modules 3 and 4 vanish.
    Boot AMS
    Status Should Become    online    ${ALL_MODULES}
    Execute Command    ${CHAIN}.cells5 BreakDownstream true
    Status Should Become    online    0x07
    Execute Command    ${CHAIN}.cells5 BreakDownstream false
    Status Should Become    online    ${ALL_MODULES}
