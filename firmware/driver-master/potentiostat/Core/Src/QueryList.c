/*
 * ModbusRTU.c
 *
 * Created: 15/08/2023
 *  Author: deniz
 */

#include <stdint.h>
#include <time.h>
#include <stddef.h>
#include <string.h>
#include "user.h"

/* Variables added to query list must be declared as external due to
	constant value is required*/

extern struct tm sys_dtime;
extern stDeviceConfig dev_cfg;
extern stAnCfg p_ancfg[eANCH_MAX];
//Matches the range main() selects at boot, so a gain read before any gain
//write reports the resistor that is really switched in
uint8_t gain_val = eTIA_SW_GAIN_100;
uint8_t switch_val = eSWITCH_OFF;
float analog_reg[eANCH_MAX];
float dac_output[eDAC_MAX];

//Batch related variables
uint32_t buffer_ind = 0;
uint32_t cmd_ind = 0;
uint32_t cmd_byte_ind = 0;
uint32_t cmd_count = 0;
uint32_t cmd_step_rx_size = 0;
uint32_t cmd_step_tx_size = 0;
uint32_t cmd_step_delay = 0;
//Step k of a batch is due at its start tick + k * cmd_step_delay, however late
//earlier steps ran, so one slow loop pass cannot shift every later point
uint32_t cmd_due_tick = 0;
uint8_t cmd_rx_buffer[CMD_RX_BUFFER_SIZE] = {0};
uint8_t cmd_tx_buffer[CMD_TX_BUFFER_SIZE] = {0};

//Batch supply bookkeeping: how many waveform bytes the host has streamed in
//(monotonic, reset by CMD_BUFFER_RESET) versus how many the batch stepper has
//consumed (cmd_ind * cmd_step_rx_size). The stepper must never read past what
//the host has written — doing so replays stale bytes from an earlier lap of
//the ring buffer, silently applying wrong potentials to the cell.
uint32_t batch_bytes_written = 0;
uint32_t batch_starved_steps = 0; //step slots spent waiting for the host
uint32_t batch_rejected_setpoints = 0; //corrupted setpoints that ended a batch

//Per-batch options, the optional third EXECUTE word. Hosts that send two words
//get neither, which is the historical behaviour.
//SAMPLE_AT_END: read the cell at the end of each setpoint's dwell, just before
//the next one is written (the staircase/pulse sampling convention, where the
//charging current has decayed), instead of right after the setpoint is
//written, when every DAC step's charging spike lands in the reading.
//STARVE_FAILSAFE: if the host stops feeding the waveform for
//BATCH_STARVE_ABORT_MS (it froze, crashed or lost the port mid-run), end the
//batch with the cell disconnected instead of holding the last potential
//indefinitely. Opt-in, because a host that restarts a failed stream without
//reconnecting the cell (the alternating-polarity loop) would carry on with it
//disconnected.
//AUTO_RANGE (with SAMPLE_AT_END): move between current ranges as the current
//changes, only as a step begins, so each end-of-step reading is taken on the
//range it has settled on, and only at points that are a multiple of the
//period in the flag word's top 16 bits (a DPV passes its step-plus-pulse
//length, so both readings of a pulse pair share a range).
//IR_COMP (with SAMPLE_AT_END): add the ohmic drop back onto each setpoint,
//from the previous step's end-of-step reading, as configured by
//CMD_IR_CONFIG (see Ir_Update). Per batch, so a configuration left behind by
//an earlier run never applies to a plain one.
#define BATCH_FLAG_SAMPLE_AT_END   (1u << 0)
#define BATCH_FLAG_STARVE_FAILSAFE (1u << 1)
#define BATCH_FLAG_AUTO_RANGE      (1u << 2)
#define BATCH_FLAG_IR_COMP         (1u << 3)
#define BATCH_RANGE_PERIOD_SHIFT   16
uint32_t batch_flags = 0;
uint8_t batch_sample_pending = 0; //SAMPLE_AT_END: a setpoint is applied and its sample is due
uint8_t batch_auto_range = 0;     //this batch changes range by itself
uint8_t batch_tag_range = 0;      //each sample carries the range it was read on
uint32_t batch_range_period = 1;  //points between allowed range changes

//Auto-range thresholds, as a share of a range's reach (the current at which
//its reading clips). A reading beyond AUTO_DOWN_FRAC, or a step's charging
//spike beyond AUTO_SPIKE_FRAC, moves to the next less sensitive range. The
//next more sensitive range is taken once every reading and spike for
//AUTO_UP_POINTS points would stay under AUTO_UP_TARGET of its reach, so a
//change never undoes itself on the next point: ranges are ~10x apart. The
//spike is held and decays slowly, because at slow scan rates the DAC only
//changes level every few dozen points.
#define AUTO_DOWN_FRAC   0.85f
#define AUTO_SPIKE_FRAC  0.95f
#define AUTO_UP_TARGET   0.60f
#define AUTO_UP_POINTS   8u
#define AUTO_SPIKE_HOLD  0.999f
//The amplifier output reaches the ADC limit at about 4.9 V, which the
//translator divides by 3
#define AUTO_REACH_ADC_V (4.9f / 3.0f)
//The current channel's reading with no current flowing, taken each time the
//cell is connected (-1 until then). It sits ~19 mV from mid-scale (a fixed
//~57 mV at the amplifier, the same on every range), so currents are measured
//from it: a share that included the offset would not scale with the range.
float auto_zero_adc = -1.0f;
float auto_max_frac = 0;         //largest reading since the last decision
float auto_spike_frac = 0;       //recent largest step spike, held
uint32_t auto_window_samples = 0;
uint32_t auto_up_points = 0;     //points in a row that fit the next range up

//On-board iR compensation (BATCH_FLAG_IR_COMP). The host reduces everything
//between a reading and the potential it calls for to one factor per current
//range: DAC volts to add per ADC volt the current channel reads away from
//auto_zero_adc. Positive feedback, so the correction is smoothed, walks at
//most ir_max_step_dac per step, and never exceeds ir_max_dac (which the host
//sets from the output's headroom beyond the sweep). Two conditions end the
//batch with the cell disconnected:
// - a runaway: an overestimated R_u makes each rise of the correction raise
//   the current by more than it, so the correction climbs at its walking
//   limit and still falls further behind what the readings call for.
//   ir_runaway_steps such steps in a row stop it. (A correction catching up,
//   as at the start of a run from a potential where current flows, also
//   walks at its limit, but gains on its target every step.)
// - the correction held at its cap, or the output at the DAC's end, for
//   ir_limit_steps steps in a row: the board cannot deliver the sweep.
float ir_k[5] = {0, 0, 0, 0, 0};
float ir_max_dac = 0;
float ir_max_step_dac = 0;
float ir_alpha = 1.0f;
uint32_t ir_runaway_steps = 1;
uint32_t ir_limit_steps = 1;
uint8_t batch_ir = 0;            //this batch compensates
float ir_filtered = 0;           //smoothed correction the readings call for
float ir_correction = 0;         //correction applied to the next setpoint
float ir_last_gap = 0;           //how far the correction trailed its target
uint32_t ir_runaway_run = 0;     //steps in a row that looked like a runaway
uint32_t ir_pinned_run = 0;      //steps in a row held at a limit
//Run report (CMD_IR_REPORT), cleared with the other per-upload counters
uint32_t ir_runs = 0;            //batches started with compensation on
uint32_t ir_clamped_steps = 0;   //steps held at the cap or the DAC's end
uint32_t ir_slewed_steps = 0;    //steps the correction trailed at its walking limit
uint32_t ir_runaway_aborts = 0;  //batches ended as runaways
uint32_t ir_limit_aborts = 0;    //batches ended held at a limit
float ir_max_abs = 0;            //largest correction applied, DAC volts

//A step later than this re-anchors the schedule instead of catching up. Transmit
//waits are capped at 50 ms (usbd_cdc_if.c), so only a far longer stall could
//otherwise make the stepper race through the points it missed.
#define BATCH_RESYNC_MS 100u
#define BATCH_STARVE_ABORT_MS 2000u

uint32_t batch_starve_run = 0;    //consecutive starved step slots
uint32_t batch_late_steps = 0;    //steps that ran at least 1 ms after they were due
uint32_t batch_max_late_ms = 0;   //worst lateness of any step
uint32_t batch_resyncs = 0;       //schedule re-anchors after a long stall
uint32_t batch_starve_aborts = 0; //batches ended because the host stopped feeding

//Capabilities announced in the diagnostics report, so the host knows how a run
//was timed and sampled
#define FW_FEATURE_SAMPLE_AT_END   (1u << 0)
#define FW_FEATURE_ABSOLUTE_TIMING (1u << 1)
#define FW_FEATURE_STARVE_FAILSAFE (1u << 2)
//Ranges 1-4 each connect one feedback resistor (1k, 10k, 91k, 1M); older
//firmware connected the wrong combinations, so the host must not rely on them
#define FW_FEATURE_TRUE_RANGES     (1u << 3)
#define FW_FEATURE_AUTO_RANGE      (1u << 4)
//CMD_DAC_STEP_CAPTURE: the R_u step made by the DAC with the cell connected
#define FW_FEATURE_DAC_STEP        (1u << 5)
//CMD_IR_CONFIG, CMD_IR_REPORT and BATCH_FLAG_IR_COMP
#define FW_FEATURE_IR_COMP         (1u << 6)

//Diagnostics sources owned by other files
extern uint32_t reset_cause_raw;  //main.c: why the MCU last rebooted
extern uint32_t cdc_tx_dropped;   //usbd_cdc_if.c: samples dropped, host not reading
extern uint32_t cdc_rx_overflow;  //usbd_cdc_if.c: received frames discarded

//Current hold loop related variables
extern float target_current_V;
extern float current_hold_adj_rate;
extern float current_hold_step_V;
extern float old_current_V;
extern uint8_t current_hold_active;
extern uint8_t auto_gain;

uint8_t ok_resp[] = {'O','K','\r','\n'};
uint8_t err_resp[] = {'E','R','R','\r','\n'};

osTimerId_t command_tmr = NULL;
/* Process functions definition */
int32_t ProcessAnalogRead(void *pdata, uint32_t data_len);
int32_t ProcessDacWrite(void *pdata, uint32_t data_len);
int32_t ProcessDacRead(void *pdata, uint32_t data_len);
int32_t ProcessTiaGainWrite(void *pdata, uint32_t data_len);
int32_t ProcessTiaGainRead(void *pdata, uint32_t data_len);
int32_t ProcessSwitchWrite(void *pdata, uint32_t data_len);
int32_t ProcessSwitchRead(void *pdata, uint32_t data_len);
int32_t ProcessDateTimeCfg(void *, uint32_t );
int32_t ProcessDacExecuteBatch(void *pdata, uint32_t data_len);
int32_t ProcessBufferWrite(void *pdata, uint32_t data_len);
int32_t ProcessBufferReset(void *pdata, uint32_t data_len);
int32_t ProcessBufferRead(void *pdata, uint32_t data_len);
int32_t ProcessCurrentHold(void *pData, uint32_t data_len);
int32_t ProcessSampleCount(void *pData, uint32_t data_len);
int32_t ProcessAutoGainWrite(void *pData, uint32_t data_len);
int32_t ProcessAutoGainRead(void *pData, uint32_t data_len);
int32_t ProcessAnalogGainRead(void *pData, uint32_t data_len);
int32_t ProcessCurrentHoldStop(void *pData, uint32_t data_len);
int32_t ProcessDiagnosticsRead(void *pData, uint32_t data_len);

uint32_t dummy;
const serialQuery rdCfg_List[]={
	{CMD_ANALOG_READ,			sizeof(analog_reg),					&analog_reg,					ProcessAnalogRead},
	{CMD_DAC_WRITE,				sizeof(dac_output),					&dac_output,					ProcessDacWrite},
	{CMD_DAC_READ,				sizeof(dac_output),					&dac_output,					ProcessDacRead},
	{CMD_SWITCH_WRITE,			sizeof(switch_val),					&switch_val,					ProcessSwitchWrite},
	{CMD_TIA_GAIN_WRITE,		sizeof(gain_val),					&gain_val,						ProcessTiaGainWrite},
	{CMD_CFG_DATETIME, 			sizeof(struct tm), 					&sys_dtime, 					&ProcessDateTimeCfg},
	{CMD_DAC_EXECUTE_BATCH, 	0, 									NULL, 							ProcessDacExecuteBatch},
	{CMD_BUFFER_WRITE, 			0, 									&cmd_rx_buffer, 				ProcessBufferWrite},
	{CMD_BUFFER_READ, 			0, 									&cmd_tx_buffer, 				ProcessBufferRead},
	{CMD_CURRENT_HOLD, 			0, 									&analog_reg, 					ProcessCurrentHold},
	{CMD_BUFFER_RESET, 			0, 									&cmd_rx_buffer, 				ProcessBufferReset},
	{CMD_SAMPLE_COUNT, 			0, 									&analog_sample_count, 			ProcessSampleCount},
	{CMD_AUTO_GAIN_WRITE, 		0, 									&gain_val, 						ProcessAutoGainWrite},
	{CMD_ANALOG_GAIN_READ, 		0, 									NULL, 							ProcessAnalogGainRead},
	{CMD_AUTO_GAIN_READ, 		0, 									NULL, 							ProcessAutoGainRead},
	{CMD_TIA_GAIN_READ, 		0, 									NULL, 							ProcessTiaGainRead},
	{CMD_SWITCH_READ, 			0, 									NULL, 							ProcessSwitchRead},
	{CMD_CURRENT_HOLD_STOP, 	0, 									NULL, 							ProcessCurrentHoldStop},
	{CMD_DIAGNOSTICS_READ, 		0, 									NULL, 							ProcessDiagnosticsRead},
	{CMD_STEP_CAPTURE, 			0, 									NULL, 							ProcessStepCapture},
	{CMD_DAC_STEP_CAPTURE, 		0, 									NULL, 							ProcessDacStepCapture},
	{CMD_IR_CONFIG, 			0, 									NULL, 							ProcessIrConfig},
	{CMD_IR_REPORT, 			0, 									NULL, 							ProcessIrReport},

};

uint32_t GetListSize(void)
{
	return (sizeof(rdCfg_List) / sizeof(serialQuery));
}

serialQuery *Get_CfgList(void)
{
	return (serialQuery *)rdCfg_List;
}

int32_t ProcessAnalogRead(void *pData, uint32_t data_len){
	static float response[eANCH_MAX];
	uint8_t *adc_list = pData; //each item is index of one of the target channel
	Update_ADC_Readings(adc_list, data_len);

	for (uint32_t i = 0; i < data_len; i++){ //for each channel
		response[i] = GetAnalogData(adc_list[i])->mean_val;
	}
	//for(uint32_t x = 0; x < eANCH_MAX; analog_reg[x] = p_an[x].mean_val, x++);
	CDC_Transmit_FS((uint8_t *)response,data_len * 4); //sending 32bit floats for each 8bit int channel index
	return NO_ERROR;
}

int32_t ProcessAnalogGainRead(void *pData, uint32_t data_len){
	static uint8_t response[(eANCH_MAX*4)+1];
	float *p_resp_an = (float *)response;
	uint8_t *p_resp_gain = response + (data_len * 4);
	uint8_t *adc_list = pData; //each item is index of one of the target channel
	Update_ADC_Readings(adc_list, data_len);
	for (uint32_t i = 0; i < data_len; i++){ //for each channel
		p_resp_an[i] = GetAnalogData(adc_list[i])->mean_val;
	}
	p_resp_gain[0] = gain_val;
	CDC_Transmit_FS((uint8_t *)response,(data_len * 4) + 1); //sending float32 + uint8 gain (5 bytes) for each 8bit int channel index
	return NO_ERROR;
}

int32_t ProcessCurrentHold(void *pData, uint32_t data_len){
	float *p_val = pData;
	uint8_t ch[] = {1,2};
	cmd_count = 0; //stop batch job by setting target number of jobs to 0
	Update_ADC_Readings(ch, 2);
	target_current_V = p_val[0]; //V
	current_hold_step_V = p_val[1]; //V
	current_hold_adj_rate = p_val[2]; //unitless multiplier like learning rate
	Update_ADC_Readings(ch, 2);
	current_hold_active = 1;
	CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
	return NO_ERROR;
}

int32_t ProcessCurrentHoldStop(void *pData, uint32_t data_len) {
	current_hold_active = 0;
	CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
	return NO_ERROR;
}

int32_t ProcessDacWrite(void *pData, uint32_t data_len){
	uint8_t ch_count = data_len / 5; //1 byte for channel ind + 4 byte for channel val
	uint8_t *p_ch = (uint8_t *)pData;
	float *p_val = (float *)(p_ch + ch_count);
	for (uint8_t i = 0; i < ch_count; i++){
		volatile uint8_t a = p_ch[i];
		volatile float b = p_val[i];
		//Set_DAC_Value(p_ch[i], p_val[i]);
		Set_DAC_Value(a, b);
	}
	//memcpy(dac_output,pData,data_len);
	//for(uint32_t x = 0; x < eDAC_MAX; Set_DAC_Value(x, dac_output[x]), x++);
	CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
	return NO_ERROR;
}

int32_t ProcessDacRead(void *pData, uint32_t data_len){
	stDAC_Data *p_dac = GetDACData();
	static float response[eDAC_MAX];
	uint8_t *dac_list = pData; //each item is index of one of the target channel
	for (uint32_t i = 0; i < data_len; i++){ //for each channel
		response[i] = p_dac[dac_list[i]].set_val;
	}
	CDC_Transmit_FS((uint8_t *)response,data_len * 4); //for each channel ind (8bit), sending float(32bit)
	return NO_ERROR;
}

int32_t ProcessSwitchWrite(void *pData, uint32_t data_len)
{
	uint8_t requested = *(uint8_t *)pData;
	if (requested > 1) {
		requested = 0;
	}
	//Just before the cell connects no current can flow, so this reading is
	//the current channel's zero that auto-ranging measures currents from
	if ((requested == eSWITCH_ON) && (switch_val == eSWITCH_OFF)) {
		uint8_t ch_list[] = {eANCH_WEOUT};
		Update_ADC_Readings(ch_list, 1);
		auto_zero_adc = GetAnalogData(eANCH_WEOUT)->mean_val;
	}
	switch_val = requested;
	HAL_GPIO_WritePin(CE_EN_GPIO_Port, CE_EN_Pin, switch_val);
	CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
	return NO_ERROR;
}

int32_t ProcessSwitchRead(void *pData, uint32_t data_len)
{
	CDC_Transmit_FS(&switch_val,sizeof(switch_val));
	return NO_ERROR;
}

int32_t ProcessTiaGainWrite(void *pData, uint32_t data_len)
{
	uint32_t Cmd_Error = NO_ERROR;
	gain_val = *(uint8_t *)pData;
	if(gain_val > eTIA_SW_GAIN_1M) {
		Cmd_Error = ILLEGAL_DATA_VALUE;
	} else {
		Set_TIA_Gain((eTIA_GAIN)gain_val);
	}
	CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
	return Cmd_Error;
}

int32_t ProcessTiaGainRead(void *pData, uint32_t data_len)
{
	CDC_Transmit_FS(&gain_val,sizeof(gain_val));
	return NO_ERROR;
}

int32_t ProcessSampleCount(void *pData, uint32_t data_len)
{
	analog_sample_count = *(uint32_t *)pData;
	CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
	return NO_ERROR;
}

int32_t ProcessAutoGainWrite(void *pData, uint32_t data_len)
{
	auto_gain = *(uint8_t *)pData;
	CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
	return NO_ERROR;
}

int32_t ProcessAutoGainRead(void *pData, uint32_t data_len)
{
	CDC_Transmit_FS(&auto_gain,sizeof(auto_gain));
	return NO_ERROR;
}


int32_t ProcessDateTimeCfg(void *pData, uint32_t data_len)
{
	SetDatetimeToInternalRTC(pData);
	CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
	return NO_ERROR;
}

int32_t ProcessBufferWrite(void *pData, uint32_t data_len){
	uint8_t *p_data = pData;
	/* Waveform points are 4-byte floats, so a length that is not a whole
	 * number of them means this frame was cut short or merged with another
	 * in transit. Appending it would move every later point off its 4-byte
	 * boundary (garbage setpoints until the ring wraps); dropping it keeps
	 * the ring aligned, and the starvation guard absorbs the missing points. */
	if ((data_len % 4) != 0) {
		cdc_rx_overflow++;
		return ILLEGAL_DATA_SIZE;
	}
	if ((buffer_ind + data_len) > CMD_RX_BUFFER_SIZE) {
		buffer_ind = 0;
	}
	uint8_t *p_target = cmd_rx_buffer;
	p_target += buffer_ind;
	memcpy(p_target,p_data,data_len);
	buffer_ind += data_len;
	batch_bytes_written += data_len; //tell the batch stepper more data is available
	return NO_ERROR;
}

int32_t ProcessBufferWriteAt(void *pData, uint32_t data_len){
	/* pData: [u16 write position][waveform bytes]. The host names the exact
	 * ring position, so a dropped or truncated message costs one stale span
	 * (the stepper replays the previous lap there) instead of shifting the
	 * alignment of everything written after it. No response is transmitted,
	 * matching CMD_BUFFER_WRITE, to keep the TX path free for batch data. */
	uint8_t *p = pData;
	if (data_len < 2) return ILLEGAL_DATA_SIZE;
	uint32_t w_pos = (uint32_t)p[0] | ((uint32_t)p[1] << 8);
	uint32_t n = data_len - 2;
	if ((w_pos + n) > CMD_RX_BUFFER_SIZE) return ILLEGAL_DATA_VALUE;
	/* The host only ever writes whole floats at float boundaries; anything
	 * else is a corrupted frame, and writing it would plant a mis-registered
	 * point in the ring. */
	if (((w_pos % 4) != 0) || ((n % 4) != 0)) {
		cdc_rx_overflow++;
		return ILLEGAL_DATA_VALUE;
	}
	memcpy(cmd_rx_buffer + w_pos, p + 2, n);
	batch_bytes_written += n; //tell the batch stepper more data is available
	return NO_ERROR;
}

int32_t ProcessBufferReset(void *pData, uint32_t data_len){
	uint32_t new_ind = *((uint32_t *)pData);
	if (new_ind < CMD_RX_BUFFER_SIZE){
		buffer_ind = new_ind;
		//A buffer reset starts a fresh waveform upload: restart the supply
		//bookkeeping with it (the host always resets to position 0).
		batch_bytes_written = new_ind;
		batch_starved_steps = 0;
		batch_rejected_setpoints = 0;
		batch_late_steps = 0;
		batch_max_late_ms = 0;
		batch_resyncs = 0;
		batch_starve_aborts = 0;
		ir_runs = 0;
		ir_clamped_steps = 0;
		ir_slewed_steps = 0;
		ir_runaway_aborts = 0;
		ir_limit_aborts = 0;
		ir_max_abs = 0;
		CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
		return NO_ERROR;
	}
	return ILLEGAL_DATA_VALUE;
}

int32_t ProcessBufferRead(void *pData, uint32_t data_len){
	uint32_t *pPacket = (uint32_t *)pData;
	uint32_t buffer_read_start = pPacket[0];
	uint32_t buffer_read_count = pPacket[1];
	if ((buffer_read_start + buffer_read_count) > CMD_TX_BUFFER_SIZE){
		CDC_Transmit_FS(err_resp,sizeof(err_resp));
		return ILLEGAL_DATA_SIZE;
	}
	CDC_Transmit_FS(cmd_tx_buffer + buffer_read_start,buffer_read_count);
	return NO_ERROR;
}

int32_t ProcessDacExecuteBatch(void *pData, uint32_t data_len){
	/* [u32 step delay ms][u32 step count][optional u32 BATCH_FLAG_* options].
	 * A count of 0 halts the running batch. The words arrive unaligned in the
	 * frame buffer, hence the copies. */
	uint32_t words[3] = {0, 0, 0};
	if (data_len < 8) return ILLEGAL_DATA_SIZE; //a cut-off command must not start a batch
	memcpy(words, pData, (data_len >= 12) ? 12 : 8);
	cmd_step_delay = words[0];
	cmd_count = words[1];
	batch_flags = words[2] & (BATCH_FLAG_SAMPLE_AT_END | BATCH_FLAG_STARVE_FAILSAFE
	                          | BATCH_FLAG_AUTO_RANGE | BATCH_FLAG_IR_COMP);
	batch_range_period = words[2] >> BATCH_RANGE_PERIOD_SHIFT;
	if (batch_range_period == 0) batch_range_period = 1;
	//Changing range needs the step's dwell to settle before its reading
	batch_auto_range = (batch_flags & BATCH_FLAG_AUTO_RANGE)
	                   && (batch_flags & BATCH_FLAG_SAMPLE_AT_END);
	batch_tag_range = auto_gain || batch_auto_range;
	//Compensation needs a settled end-of-step reading, the zero it is
	//measured from, and a configuration
	batch_ir = (batch_flags & BATCH_FLAG_IR_COMP)
	           && (batch_flags & BATCH_FLAG_SAMPLE_AT_END)
	           && (auto_zero_adc >= 0) && (ir_max_dac > 0);
	if (batch_ir && (cmd_count > 0)) ir_runs++;
	ir_filtered = 0;
	ir_correction = 0;
	ir_last_gap = 0;
	ir_runaway_run = 0;
	ir_pinned_run = 0;
	auto_max_frac = 0;
	auto_spike_frac = 0;
	auto_window_samples = 0;
	auto_up_points = 0;
	batch_sample_pending = 0;
	batch_starve_run = 0;
	cmd_step_rx_size = 4;
	cmd_step_tx_size = 8;
	if (batch_tag_range) {cmd_step_tx_size++;} //1 byte for the range index
	cmd_ind = 0;
	cmd_byte_ind = 0;
	current_hold_active = 0; //disable current hold if active
	CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
	//The schedule starts once the acknowledgement is out, so the first step
	//runs at once and the time spent sending it is not counted as lateness
	cmd_due_tick = HAL_GetTick();
	return NO_ERROR;
}

static void Batch_Abort_Disconnect(void){
	//The DAC keeps the last valid setpoint; the host sees the stream stop and
	//the diagnostics report why
	cmd_count = 0;
	batch_sample_pending = 0;
	HAL_GPIO_WritePin(CE_EN_GPIO_Port, CE_EN_Pin, GPIO_PIN_RESET);
	switch_val = eSWITCH_OFF;
}

/* Apply the batch's next setpoint. Returns 1 when applied, 0 when the host has
 * not sent it yet (the present potential is held for this slot), and -1 when
 * the batch was ended with the cell disconnected. */
static int32_t Batch_Apply_Next(void){
	//Starvation guard: never step past the data the host has actually sent.
	//Without this, a lagging host made the stepper read stale bytes from an
	//earlier lap of the ring buffer — applying old potentials to the cell
	//with no visible error. Instead, hold the present potential for this
	//step slot and count it so the host can see the run was degraded.
	if ((batch_bytes_written - (cmd_ind * cmd_step_rx_size)) < cmd_step_rx_size) {
		batch_starved_steps++;
		batch_starve_run++;
		if ((batch_flags & BATCH_FLAG_STARVE_FAILSAFE)
		        && ((batch_starve_run * cmd_step_delay) >= BATCH_STARVE_ABORT_MS)) {
			batch_starve_aborts++;
			Batch_Abort_Disconnect();
			return -1;
		}
		return 0;
	}
	batch_starve_run = 0;
	float setpoint;
	memcpy(&setpoint, cmd_rx_buffer + cmd_byte_ind, sizeof(setpoint));
	//Every setpoint the host sends lies within the DAC's 0-3.3 V span (the
	//driver clips to it). Anything else, NaN included (it fails both
	//comparisons), is corrupted ring data such as floats read off their byte
	//boundary, and applying it swings the output rail to rail. End the batch
	//with the cell disconnected instead.
	if (!((setpoint >= 0.0f) && (setpoint <= (float)ADCVREF))) {
		batch_rejected_setpoints++;
		Batch_Abort_Disconnect();
		return -1;
	}
	if (batch_ir) {
		float applied = setpoint + ir_correction;
		uint8_t pinned = (ir_correction >= ir_max_dac) || (ir_correction <= -ir_max_dac);
		if (applied < 0.0f) {
			applied = 0.0f;
			pinned = 1;
		} else if (applied > (float)ADCVREF) {
			applied = (float)ADCVREF;
			pinned = 1;
		}
		if (pinned) {
			ir_clamped_steps++;
			if (++ir_pinned_run >= ir_limit_steps) {
				ir_limit_aborts++;
				Batch_Abort_Disconnect();
				return -1;
			}
		} else {
			ir_pinned_run = 0;
		}
		float size = (ir_correction < 0) ? -ir_correction : ir_correction;
		if (size > ir_max_abs) ir_max_abs = size;
		setpoint = applied;
	}
	if (setpoint != Get_DAC_Value(eDAC_VCEIN)){
		Set_DAC_Value(eDAC_VCEIN, setpoint); //only if a new V is targeted
	}
	cmd_ind++;
	cmd_byte_ind += cmd_step_rx_size;
	if (cmd_byte_ind + cmd_step_rx_size > CMD_RX_BUFFER_SIZE){
		cmd_byte_ind = 0;
	}
	return 1;
}

static void Batch_Sample(float *analog_results){
	uint8_t ch_list[] = {eANCH_WEOUT,eANCH_REOUT};
	Update_ADC_Readings(ch_list, 2);
	analog_results[0] = GetAnalogData(eANCH_WEOUT)->mean_val; //WE out
	analog_results[1] = GetAnalogData(eANCH_REOUT)->mean_val; //RE out
	if (batch_tag_range){
		//the range this step was read on
		uint8_t *p_gain = (uint8_t *)(analog_results + 2);
		p_gain[0] = gain_val;
	}
}

/* The correction the next setpoint gets, from this step's end-of-step
 * reading, taken on the range the step ran on. One step late by design: the
 * reading is of the step just finished. Ends the batch, with the cell
 * disconnected, on a runaway. */
static void Ir_Update(float we_out){
	float wanted = ir_k[(gain_val < 5) ? gain_val : 4] * (we_out - auto_zero_adc);
	ir_filtered += ir_alpha * (wanted - ir_filtered);
	float next = ir_filtered;
	uint8_t slewed = 0;
	if (next > ir_correction + ir_max_step_dac) {
		next = ir_correction + ir_max_step_dac;
		slewed = 1;
	} else if (next < ir_correction - ir_max_step_dac) {
		next = ir_correction - ir_max_step_dac;
		slewed = 1;
	}
	if (next > ir_max_dac) next = ir_max_dac;
	if (next < -ir_max_dac) next = -ir_max_dac;

	float size_now = (ir_correction < 0) ? -ir_correction : ir_correction;
	float size_next = (next < 0) ? -next : next;
	float gap = ir_filtered - next;
	if (gap < 0) gap = -gap;
	if (slewed) ir_slewed_steps++;
	if (slewed && (size_next > size_now) && (gap >= ir_last_gap)) {
		if (++ir_runaway_run >= ir_runaway_steps) {
			ir_runaway_aborts++;
			Batch_Abort_Disconnect();
		}
	} else {
		ir_runaway_run = 0;
	}
	ir_last_gap = slewed ? gap : 0;
	ir_correction = next;
}

/* Share of the present range's reach the current in a WE_OUT reading (ADC
 * volts) uses: 0 at zero current, about 1 where the reading clips. */
static float Range_Fraction(float we_out){
	float zero = (auto_zero_adc >= 0) ? auto_zero_adc : Get_DAC_Value(eDAC_VANA2);
	float frac = (we_out - zero) / AUTO_REACH_ADC_V;
	return (frac < 0) ? -frac : frac;
}

static void Batch_Set_Range(uint8_t index){
	//The held spike is a share of the old range's reach
	auto_spike_frac *= TIA_Gain_Ohms((eTIA_GAIN)index) / TIA_Gain_Ohms((eTIA_GAIN)gain_val);
	gain_val = index;
	Set_TIA_Gain((eTIA_GAIN)index);
}

/* Decide the range for the step about to begin, from the readings and spikes
 * since the last decision. Returns 1 if the range changed. */
static uint8_t Batch_Auto_Range(void){
	uint8_t changed = 0;
	if (auto_window_samples == 0) return 0;
	float worst = (auto_max_frac > auto_spike_frac) ? auto_max_frac : auto_spike_frac;
	if (((auto_max_frac > AUTO_DOWN_FRAC) || (auto_spike_frac > AUTO_SPIKE_FRAC))
	        && (gain_val > eTIA_SW_GAIN_100)) {
		Batch_Set_Range(gain_val - 1);
		auto_up_points = 0;
		changed = 1;
	} else if (gain_val < eTIA_SW_GAIN_1M) {
		float ratio = TIA_Gain_Ohms((eTIA_GAIN)(gain_val + 1)) / TIA_Gain_Ohms((eTIA_GAIN)gain_val);
		if ((worst * ratio) < AUTO_UP_TARGET) {
			auto_up_points += auto_window_samples;
			if (auto_up_points >= AUTO_UP_POINTS) {
				Batch_Set_Range(gain_val + 1);
				auto_up_points = 0;
				changed = 1;
			}
		} else {
			auto_up_points = 0;
		}
	}
	auto_max_frac = 0;
	auto_window_samples = 0;
	return changed;
}

int32_t ProcessDacWriteBatchStep(){
	float analog_results[3];
	uint8_t have_sample = 0;
	if (cmd_count == 0) return 0; //no batch, or halted
	if ((cmd_ind >= cmd_count) && !batch_sample_pending) return 0; //finished
	uint32_t tick_now = HAL_GetTick();
	int32_t late_ms = (int32_t)(tick_now - cmd_due_tick);
	if (late_ms < 0) return 0; //not due yet

	//Keep the steps on their absolute grid: a step that ran late (e.g. behind
	//a slow transmit) is followed by one that is back on time, so a delay no
	//longer carries into every later point. Only a stall far beyond any
	//normal wait re-anchors the grid; both are counted for the diagnostics.
	if (late_ms > 0) {
		batch_late_steps++;
		if ((uint32_t)late_ms > batch_max_late_ms) batch_max_late_ms = (uint32_t)late_ms;
	}
	if ((uint32_t)late_ms > BATCH_RESYNC_MS) {
		batch_resyncs++;
		cmd_due_tick = tick_now + cmd_step_delay;
	} else {
		cmd_due_tick += cmd_step_delay;
	}

	//End-of-dwell sample of the setpoint applied one step ago, taken before
	//the output changes
	if (batch_sample_pending) {
		Batch_Sample(analog_results);
		batch_sample_pending = 0;
		have_sample = 1;
		if (batch_ir) Ir_Update(analog_results[0]);
		if (batch_auto_range) {
			float frac = Range_Fraction(analog_results[0]);
			if (frac > auto_max_frac) auto_max_frac = frac;
			auto_window_samples++;
		}
	}
	if (cmd_ind < cmd_count) {
		//A range change happens as the step begins, together with its setpoint
		uint8_t range_changed = 0;
		if (batch_auto_range && ((cmd_ind % batch_range_period) == 0)) {
			range_changed = Batch_Auto_Range();
		}
		if (Batch_Apply_Next() == 1) {
			//The step's charging spike, unless the range switch itself just
			//disturbed the reading
			if (batch_auto_range && !range_changed) {
				float we_out = Read_WEOUT_Fast();
				auto_spike_frac *= AUTO_SPIKE_HOLD;
				if (we_out >= 0) {
					float frac = Range_Fraction(we_out);
					if (frac > auto_spike_frac) auto_spike_frac = frac;
				}
			}
			if (batch_flags & BATCH_FLAG_SAMPLE_AT_END) {
				batch_sample_pending = 1;
			} else {
				Batch_Sample(analog_results); //historical: sample right after setting
				have_sample = 1;
			}
		}
	}
	//Sent after the new setpoint is out, so a slow transmit never delays it
	if (have_sample) {
		CDC_Transmit_FS((uint8_t *)analog_results,cmd_step_tx_size);
	}
	return have_sample;
}


int32_t ProcessStepCapture(void *pData, uint32_t data_len){
	/* [u32 n_samples][u32 period_us] -> StepCapture() in analog.c: close the
	 * CE switch and burst-sample WE_OUT raw counts into cmd_tx_buffer.
	 * Requests are clamped, never refused, so the host always gets the
	 * 9-byte response and reads the truth from the 'captured' count:
	 * [u32 captured][u32 actual_duration_us][u8 gain].
	 * The trace is then collected with CMD_BUFFER_READ. The switch is left
	 * closed on success for the host's spike-free disconnect. */
	static uint8_t response[9];
	if (data_len < 8) return ILLEGAL_DATA_SIZE;
	uint32_t *p = pData;
	uint32_t n_samples = p[0];
	uint32_t period_us = p[1];
	if (period_us < 25) period_us = 25; /* per-sample overhead floor */
	uint32_t max_by_buf = CMD_TX_BUFFER_SIZE / 2;
	uint32_t max_by_time = 1000000UL / period_us; /* 1 s cap: watchdog margin */
	if (n_samples > max_by_buf) n_samples = max_by_buf;
	if (n_samples > max_by_time) n_samples = max_by_time;
	cmd_count = 0; /* stop any batch job: the capture owns the ADC */
	current_hold_active = 0; /* and any current hold: it owns the DAC */
	uint32_t duration_us = 0;
	uint32_t captured = StepCapture(n_samples, period_us, cmd_tx_buffer,
	                                &duration_us);
	memcpy(&response[0], &captured, 4);
	memcpy(&response[4], &duration_us, 4);
	response[8] = gain_val;
	CDC_Transmit_FS(response, sizeof(response));
	return NO_ERROR;
}

int32_t ProcessDacStepCapture(void *pData, uint32_t data_len){
	/* [u32 n_samples][u32 period_us][u32 n_pre][f32 dac_V] ->
	 * DacStepCapture() in analog.c: with the cell already connected, sample
	 * n_pre raw WE_OUT readings, move the CE DAC to dac_V (0-3.3 V, as
	 * CMD_DAC_WRITE takes it), and keep sampling. No switch closes, so the
	 * trace carries no amplifier wind-up recovery, only the cell's response
	 * to the step. Requests are clamped like CMD_STEP_CAPTURE's, and the
	 * host always gets the 13-byte response:
	 * [u32 captured][u32 actual_duration_us][u8 gain][u32 centre_ns],
	 * centre_ns being the time from the DAC write to the middle of the first
	 * conversion after it. captured is 0 when the cell is not connected:
	 * the capture never closes the switch itself. The DAC is left at dac_V
	 * and the switch closed. */
	static uint8_t response[13];
	if (data_len < 16) return ILLEGAL_DATA_SIZE;
	/* Copied out rather than read in place: the payload sits two bytes into
	 * the frame, and a float load from an unaligned address faults */
	uint32_t args[3];
	float dac_v;
	memcpy(args, pData, sizeof(args));
	memcpy(&dac_v, (uint8_t *)pData + sizeof(args), sizeof(dac_v));
	uint32_t n_samples = args[0];
	uint32_t period_us = args[1];
	uint32_t n_pre = args[2];
	if (period_us < 25) period_us = 25; /* per-sample overhead floor */
	uint32_t max_by_buf = CMD_TX_BUFFER_SIZE / 2;
	uint32_t max_by_time = 1000000UL / period_us; /* 1 s cap: watchdog margin */
	if (n_samples > max_by_buf) n_samples = max_by_buf;
	if (n_samples > max_by_time) n_samples = max_by_time;
	if (n_samples > 0 && n_pre >= n_samples) n_pre = n_samples - 1;
	if (!(dac_v >= 0.0f)) dac_v = 0.0f; /* also catches NaN */
	if (dac_v > 3.3f) dac_v = 3.3f;
	uint32_t captured = 0;
	uint32_t duration_us = 0;
	uint32_t centre_ns = 0;
	if (switch_val == eSWITCH_ON && n_samples > 0) {
		cmd_count = 0; /* stop any batch job: the capture owns the ADC */
		current_hold_active = 0; /* and any current hold: it owns the DAC */
		captured = DacStepCapture(n_samples, period_us, n_pre, dac_v,
		                          cmd_tx_buffer, &duration_us, &centre_ns);
	}
	memcpy(&response[0], &captured, 4);
	memcpy(&response[4], &duration_us, 4);
	response[8] = gain_val;
	memcpy(&response[9], &centre_ns, 4);
	CDC_Transmit_FS(response, sizeof(response));
	return NO_ERROR;
}

static int Finite_F(float x){
	return (x == x) && ((x - x) == 0.0f); /* false for NaN and +/-inf */
}

int32_t ProcessIrConfig(void *pData, uint32_t data_len){
	/* [f32 k[5]][f32 max_dac][f32 max_step_dac][f32 alpha]
	 * [u32 runaway_steps][u32 limit_steps] -> the on-board iR compensation a
	 * batch with BATCH_FLAG_IR_COMP uses (see Ir_Update and
	 * Batch_Apply_Next): k per current range, in DAC volts per ADC volt; the
	 * largest correction and the largest change of it per step, in DAC
	 * volts; the smoothing weight of each new reading (0-1]; and how many
	 * steps in a row of a runaway, and of a correction or output held at its
	 * limit, end the batch. Anything out of range is refused without an
	 * acknowledgement and changes nothing. */
	if (data_len < 40) return ILLEGAL_DATA_SIZE;
	float f[8];
	uint32_t steps[2];
	memcpy(f, pData, sizeof(f));
	memcpy(steps, (uint8_t *)pData + sizeof(f), sizeof(steps));
	for (uint32_t i = 0; i < 8; i++) {
		if (!Finite_F(f[i])) return ILLEGAL_DATA_VALUE;
	}
	if (!((f[5] > 0.0f) && (f[5] <= (float)ADCVREF))) return ILLEGAL_DATA_VALUE;
	if (!((f[6] > 0.0f) && (f[6] <= (float)ADCVREF))) return ILLEGAL_DATA_VALUE;
	if (!((f[7] > 0.0f) && (f[7] <= 1.0f))) return ILLEGAL_DATA_VALUE;
	if ((steps[0] == 0) || (steps[1] == 0)) return ILLEGAL_DATA_VALUE;
	memcpy(ir_k, f, sizeof(ir_k));
	ir_max_dac = f[5];
	ir_max_step_dac = f[6];
	ir_alpha = f[7];
	ir_runaway_steps = steps[0];
	ir_limit_steps = steps[1];
	CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
	return NO_ERROR;
}

int32_t ProcessIrReport(void *pData, uint32_t data_len){
	/* The compensation's run report for the current waveform upload:
	 * [u32 batches run with it][u32 steps held at the cap or the DAC's end]
	 * [u32 steps it trailed at its walking limit][u32 batches ended as
	 * runaways][u32 batches ended held at a limit]
	 * [f32 largest correction, DAC volts][f32 correction now, DAC volts] */
	static uint8_t response[28];
	memcpy(&response[0], &ir_runs, 4);
	memcpy(&response[4], &ir_clamped_steps, 4);
	memcpy(&response[8], &ir_slewed_steps, 4);
	memcpy(&response[12], &ir_runaway_aborts, 4);
	memcpy(&response[16], &ir_limit_aborts, 4);
	memcpy(&response[20], &ir_max_abs, 4);
	memcpy(&response[24], &ir_correction, 4);
	CDC_Transmit_FS(response, sizeof(response));
	return NO_ERROR;
}

int32_t ProcessDiagnosticsRead(void *pData, uint32_t data_len){
	//Health report for the host, 10 little-endian uint32 values:
	//[0] raw reset-cause flags captured at boot (bit 29 = watchdog reset,
	//    bit 28 = software reset, bit 27 = brown-out, bit 26 = pin/power-up)
	//[1] messages dropped because the host stopped reading
	//[2] received frames discarded to protect the command buffer
	//[3] batch step slots spent waiting for waveform data from the host
	//[4] corrupted setpoints that ended a batch with the cell disconnected
	//[5] batch steps that ran at least 1 ms after they were due
	//[6] the worst such lateness, in ms
	//[7] schedule re-anchors after a stall longer than BATCH_RESYNC_MS
	//[8] batches ended with the cell disconnected because the host stopped
	//    sending the waveform
	//[9] capability flags (FW_FEATURE_*)
	//[3]-[8] cover the current waveform upload (cleared by a buffer reset).
	//Older hosts read a prefix of the report and still parse it.
	//All zeros (apart from a normal power-up flag) means a healthy device.
	static uint32_t response[10];
	response[0] = reset_cause_raw;
	response[1] = cdc_tx_dropped;
	response[2] = cdc_rx_overflow;
	response[3] = batch_starved_steps;
	response[4] = batch_rejected_setpoints;
	response[5] = batch_late_steps;
	response[6] = batch_max_late_ms;
	response[7] = batch_resyncs;
	response[8] = batch_starve_aborts;
	response[9] = FW_FEATURE_SAMPLE_AT_END | FW_FEATURE_ABSOLUTE_TIMING
	            | FW_FEATURE_STARVE_FAILSAFE | FW_FEATURE_TRUE_RANGES
	            | FW_FEATURE_AUTO_RANGE | FW_FEATURE_DAC_STEP
	            | FW_FEATURE_IR_COMP;
	CDC_Transmit_FS((uint8_t *)response, sizeof(response));
	return NO_ERROR;
}


void System_Reset(void *p_data)
{
	NVIC_SystemReset();
}


