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
uint8_t gain_val = eTIA_SW_GAIN_1M;
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
uint32_t cmd_last_tick = 0;
uint8_t cmd_rx_buffer[CMD_RX_BUFFER_SIZE] = {0};
uint8_t cmd_tx_buffer[CMD_TX_BUFFER_SIZE] = {0};

//Batch supply bookkeeping: how many waveform bytes the host has streamed in
//(monotonic, reset by CMD_BUFFER_RESET) versus how many the batch stepper has
//consumed (cmd_ind * cmd_step_rx_size). The stepper must never read past what
//the host has written — doing so replays stale bytes from an earlier lap of
//the ring buffer, silently applying wrong potentials to the cell.
uint32_t batch_bytes_written = 0;
uint32_t batch_starved_steps = 0; //step slots spent waiting for the host

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
	switch_val = *(uint8_t *)pData;
	if (switch_val < 0 || switch_val > 1) {
		switch_val = 0;
	}
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
	uint32_t *pBatchData = pData;
	cmd_step_delay = pBatchData[0];
	cmd_count = pBatchData[1];
	cmd_step_rx_size = 4;
	cmd_step_tx_size = 8;
	if (auto_gain) {cmd_step_tx_size++;} //add 1 byte if auto gain
	cmd_ind = 0;
	cmd_byte_ind = 0;
	current_hold_active = 0; //disable current hold if active
	cmd_last_tick = (HAL_GetTick() - cmd_step_delay) - 1; //start immediately
	CDC_Transmit_FS(ok_resp,sizeof(ok_resp));
	return NO_ERROR;
}

int32_t ProcessDacWriteBatchStep(){
	uint8_t ch_list[] = {eANCH_WEOUT,eANCH_REOUT};
	uint8_t ch_cnt = 2;
	float analog_results[3];
	if ((cmd_ind >= cmd_count) || (cmd_count == 0)) return 0; //check whether there are commands in the queue
	int32_t result = 0; //1 if a process took place, 0 if nothing happened
	uint32_t tick_now = HAL_GetTick();
	if ((tick_now - cmd_last_tick) < cmd_step_delay) return 0; //check the delta time
	//Starvation guard: never step past the data the host has actually sent.
	//Without this, a lagging host made the stepper read stale bytes from an
	//earlier lap of the ring buffer — applying old potentials to the cell
	//with no visible error. Instead, hold the present potential for this
	//step slot and count it so the host can see the run was degraded.
	if ((batch_bytes_written - (cmd_ind * cmd_step_rx_size)) < cmd_step_rx_size) {
		batch_starved_steps++;
		cmd_last_tick = tick_now; //consume the slot; try again next period
		return 0;
	}
	uint8_t *pRxBuf = cmd_rx_buffer;
	pRxBuf += cmd_byte_ind; //set to current location in the rx array
	float *dac_buf_output = (float *)pRxBuf; //cast to float array
	//write to DAC
	cmd_last_tick = HAL_GetTick();
	if (dac_buf_output[0] != Get_DAC_Value(eDAC_VCEIN)){
		Set_DAC_Value(eDAC_VCEIN, dac_buf_output[0]); //only if a new V is targeted
	}
	//read from analog immediately
	Update_ADC_Readings(ch_list, ch_cnt);
	analog_results[0] = GetAnalogData(eANCH_WEOUT)->mean_val; //WE out
	analog_results[1] = GetAnalogData(eANCH_REOUT)->mean_val; //RE out
	if (auto_gain){
		//if auto-gain is on, 1 more byte for resistor index
		uint8_t *p_gain = (uint8_t *)(analog_results + 2);
		p_gain[0] = gain_val;
	}
	CDC_Transmit_FS((uint8_t *)analog_results,cmd_step_tx_size);
	cmd_ind++;
	cmd_byte_ind += cmd_step_rx_size;
	if (cmd_byte_ind + cmd_step_rx_size > CMD_RX_BUFFER_SIZE){
		cmd_byte_ind = 0;
	}
	result = 1;
	return result;
}


int32_t ProcessDiagnosticsRead(void *pData, uint32_t data_len){
	//Health report for the host, 4 little-endian uint32 values:
	//[0] raw reset-cause flags captured at boot (bit 29 = watchdog reset,
	//    bit 28 = software reset, bit 27 = brown-out, bit 26 = pin/power-up)
	//[1] messages dropped because the host stopped reading
	//[2] received frames discarded to protect the command buffer
	//[3] batch step slots spent waiting for waveform data from the host
	//All zeros (apart from a normal power-up flag) means a healthy device.
	static uint32_t response[4];
	response[0] = reset_cause_raw;
	response[1] = cdc_tx_dropped;
	response[2] = cdc_rx_overflow;
	response[3] = batch_starved_steps;
	CDC_Transmit_FS((uint8_t *)response, sizeof(response));
	return NO_ERROR;
}


void System_Reset(void *p_data)
{
	NVIC_SystemReset();
}


