/*
 * SerialProtocol.c
 *
 * Created: 15/08/2023
 *  Author: deniz
 */ 

#include <string.h>
#include <errno.h>
#include "user.h"

extern uint32_t rcv_ix;
uint32_t restore_dflt_tm = 0;
extern uint8_t serial_rx_buffer[SERIAL_RX_BUFFER_SIZE];
extern uint32_t cdc_rx_overflow; //usbd_cdc_if.c: discarded/truncated RX counter

const char *str_success= {"OK\r\n"};
const char *str_error_ID = {"Error_ID\r\n"};

uint32_t Compute_Serial_Cmd()
{
	uint32_t total = rcv_ix; //number of bytes received, it's kept track by this variable
	uint32_t pos = 0;
	uint32_t error = 1;
	uint8_t *p_buf = serial_rx_buffer;

	/* Walk the received frame command by command. CMD_BUFFER_WRITE_AT
	 * carries its own length, so several of them (or one followed by a
	 * legacy command) can share a frame and still parse correctly. That
	 * matters because frames used to be delimited only by >2 ms of line
	 * silence: whenever this parser ran late (e.g. blocked in a transmit
	 * wait), two waveform-feed messages landed in one frame and were
	 * processed as a single buffer write, splicing the second message's
	 * header bytes into the waveform ring — every later sample was then
	 * read off-register and the DAC railed until the ring wrapped
	 * (bench-diagnosed July 2026). Legacy commands keep the historical
	 * one-command-per-frame semantics: they consume the frame remainder. */
	while ((pos + 2) <= total) {
		uint8_t cmd_type = p_buf[pos + 1]; //byte 0 is the device ID
		if (cmd_type == CMD_BUFFER_WRITE_AT) {
			if ((pos + 4) > total) { //length field incomplete: drop tail
				cdc_rx_overflow++;
				break;
			}
			uint32_t dlen = (uint32_t)p_buf[pos + 2] | ((uint32_t)p_buf[pos + 3] << 8);
			if ((pos + 4 + 2 + dlen) > total) { //truncated message: drop tail
				cdc_rx_overflow++;
				break;
			}
			//payload = [u16 position][dlen data bytes]
			error = ProcessBufferWriteAt(&p_buf[pos + 4], dlen + 2);
			pos += 4 + 2 + dlen;
			continue;
		}
		//find the corresponding legacy command in rdCfg_List
		serialQuery *pList = Get_CfgList();
		for(uint32_t i = 0; i < GetListSize(); pList++, i++){
			if (pList->cmd_type == cmd_type) {
				//call the corresponding function
				if (pList->pfunc != NULL){
					error = pList->pfunc(&p_buf[pos + 2], total - pos - 2);
				} else {
					error = NO_ERROR;
				}
				break;
			}
		}
		pos = total; //legacy commands consume the rest of the frame
	}
	//HAL_IWDG_Refresh(&hiwdg);
	return error;
}

