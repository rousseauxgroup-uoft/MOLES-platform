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
extern uint32_t usb_rx_tmr;
uint32_t restore_dflt_tm = 0;
extern uint8_t serial_rx_buffer[SERIAL_RX_BUFFER_SIZE];
extern uint32_t cdc_rx_overflow; //usbd_cdc_if.c: discarded/truncated RX counter
extern volatile uint32_t rcv_restarts; //usbd_cdc_if.c: frame-buffer restarts after overflow

const char *str_success= {"OK\r\n"};
const char *str_error_ID = {"Error_ID\r\n"};

static void Release_Frame(uint32_t consumed, uint32_t restarts_at_capture);

void Serial_Poll(void)
{
	/* Commands arrive as USB packets of at most 64 bytes, so a longer command
	 * reaches the buffer in pieces; a frame is complete only once the line
	 * has been silent for FRAME_SILENCE_MS. The byte count and the time of
	 * the last packet are captured together, and "now" is read after them:
	 * a "now" sampled earlier in the loop pass can be older than a packet
	 * that arrived during the pass, and the unsigned difference then wraps
	 * to ~4e9. That used to end frames the instant a packet landed, cutting
	 * 66-byte CV/DPV uploads after their first 64-byte packet and shifting
	 * every later waveform point 2 bytes off its float boundary (rail-to-rail
	 * output until the ring wrapped, bench-diagnosed September 2026). */
	uint32_t primask = __get_PRIMASK();
	__disable_irq();
	uint32_t total = rcv_ix;
	uint32_t last_rx_ms = usb_rx_tmr;
	uint32_t restarts = rcv_restarts;
	__set_PRIMASK(primask);

	if (total == 0) return;
	if ((HAL_GetTick() - last_rx_ms) <= FRAME_SILENCE_MS) return;

	Compute_Serial_Cmd(total);
	Release_Frame(total, restarts);
}

static void Release_Frame(uint32_t consumed, uint32_t restarts_at_capture)
{
	/* Drop the bytes just parsed but keep any that arrived while they were
	 * being executed: they are the start of the next command, and clearing
	 * the whole buffer used to discard it. */
	uint32_t primask = __get_PRIMASK();
	__disable_irq();
	if ((rcv_restarts == restarts_at_capture) && (rcv_ix >= consumed)) {
		uint32_t extra = rcv_ix - consumed;
		if (extra) {
			memmove(serial_rx_buffer, serial_rx_buffer + consumed, extra);
		}
		rcv_ix = extra;
	}
	/* Otherwise the receive path restarted the buffer mid-parse (overflow)
	 * and it now holds only a fresh frame, which is left untouched. */
	__set_PRIMASK(primask);
}

uint32_t Compute_Serial_Cmd(uint32_t total)
{
	uint32_t pos = 0;
	uint32_t error = 1;
	uint8_t *p_buf = serial_rx_buffer;

	/* Walk the received frame command by command. CMD_BUFFER_WRITE_AT
	 * carries its own length, so several of them (or one followed by a
	 * legacy command) can share a frame and still parse correctly. That
	 * matters because frames are delimited only by line silence: whenever
	 * this parser ran late (e.g. blocked in a transmit wait), two
	 * waveform-feed messages landed in one frame and were processed as a
	 * single buffer write, splicing the second message's header bytes into
	 * the waveform ring — every later sample was then read off-register and
	 * the DAC railed until the ring wrapped (bench-diagnosed July 2026).
	 * Legacy commands keep the historical one-command-per-frame semantics:
	 * they consume the frame remainder. */
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

