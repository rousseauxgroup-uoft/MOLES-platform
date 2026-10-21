/*
 * Analog.c
 *
 *  Created on: Jan 11, 2021
 *      Author: angel
 */


#include "user.h"

extern IWDG_HandleTypeDef hiwdg;
extern ADC_HandleTypeDef hadc1, hadc3, hadc5;
extern ADC_HandleTypeDef hdac1, hdac2, hdac3;
extern uint8_t switch_val;

extern stLedConfig LED_List[LEDS_NUMBER];

//analog reading related variables
uint32_t analog_sample_count = 10;
//Batch readings carry the current range they were taken on (one extra byte).
//The host sets this; range changes themselves are the batch's auto-range
//option (QueryList.c).
uint8_t auto_gain = 1;
//Counted up by the conversion-complete interrupt while the main loop waits on
//it; volatile so the compiler re-reads it in those wait loops at any
//optimisation level instead of spinning on a stale copy
volatile uint8_t completed_ch_count = 0;

//Current hold loop related variables
float target_current_V = 1.65;
uint8_t current_hold_active = 0;
float current_hold_step_V = 0;
float old_current_V = 0;
float old_set_V = 0;
float current_hold_adj_rate = 0.01;
/* Declare 2 buffer one for DMA ping-pong operation and another for process data purposes */
/* 3 adc's 2 buffers and 3 channels for each adc */

uint16_t adc_buf[3][2][3] __attribute__ ((aligned (8)));

/* Calibrated VREFINT value at 30ºC */

float verfint_cal;
float k_adc = KADC16;

const ADC_HandleTypeDef *p_adc_ins[3] = {&hadc1, &hadc3, &hadc5};

const uint32_t adc_ch_qty[3] = {ADC1_N_CH, ADC3_N_CH, ADC5_N_CH};


/* Set ADC's and analog input settings */
/* Set *val pointer to &adc_buf[x][1][x] instead &adc_buf[x][0][x] because it's working buffer */

const stADCChn adc_ch_cfg[]={
		{&hadc1, eADC1_WEOUT, 				&adc_buf[0][1][0]},
		{&hadc3, eADC3_REOUT,				&adc_buf[1][1][0]},
		{&hadc5, eADC5_VREFINT,				&adc_buf[2][1][0]},
		{&hadc5, eADC5_MCU_TEMP, 			&adc_buf[2][1][1]},
		{&hadc5, eADC5_MCU_VBAT,			&adc_buf[2][1][2]},
};


//DISABLED. For individual slope and baseline as well as sample count of each analog channel
const stAnCfg an_cfg_dflt[]={
		[eANCH_WEOUT] = 	{.AN = 	{1.0, 0}, 	100},
		[eANCH_REOUT] =		{.AN = 	{1.0, 0}, 	100},
		[eANCH_VREFINT] =	{.AN = 	{1.0, 0}, 	10},
		[eANCH_MCU_TEMP] =	{.AN = 	{1.0, 0}, 	10},
		[eANCH_MCU_VBAT] =	{.AN = 	{3.0, 0}, 	10}
};

stAnCfg p_ancfg[eANCH_MAX];

stAnalogData an_values[] = {
		{eANCH_WEOUT, 		CollectAnalogData,				ProcessAnalogData,			(stADCChn *)&adc_ch_cfg[eANCH_WEOUT],		&p_ancfg[eANCH_WEOUT]},
		{eANCH_REOUT, 		CollectAnalogData,				ProcessAnalogData, 			(stADCChn *)&adc_ch_cfg[eANCH_REOUT],		&p_ancfg[eANCH_REOUT]},
		{eANCH_VREFINT,		CollectAnalogVREFData,			ProcessAnalogVREFData, 		(stADCChn *)&adc_ch_cfg[eANCH_VREFINT],		&p_ancfg[eANCH_VREFINT]},
		{eANCH_MCU_TEMP,	CollectAnalogSensorData,		ProcessAnalogSensorData, 	(stADCChn *)&adc_ch_cfg[eANCH_MCU_TEMP], 	&p_ancfg[eANCH_MCU_TEMP]},
		{eANCH_MCU_VBAT,	CollectAnalogSensorData,		ProcessAnalogSensorData, 	(stADCChn *)&adc_ch_cfg[eANCH_MCU_VBAT], 	&p_ancfg[eANCH_MCU_VBAT]},
};

/* Set comparator and DAC's settings */

const st_DAC_Chn dac_ch_cfg[]={
		[eDAC_VCEIN] =	{&hdac1, DAC_CHANNEL_1},
		[eDAC_TOAREF] =	{&hdac2, DAC_CHANNEL_1},
		[eDAC_VANA2] =	{&hdac3, DAC_CHANNEL_2},
};

const stDAC_Cfg dac_cfg_dflt[]={
		[eDAC_VCEIN] =	{1.0, 0,	1.65},
		[eDAC_TOAREF] =	{1.0, 0,	1.65},
		[eDAC_VANA2] = 	{1.0, 0,	1.65}
};

stDAC_Cfg p_dac_cfg[eDAC_MAX];

stDAC_Data dac_values[]={
		{eDAC_VCEIN,	ProcessDacValue,	(st_DAC_Chn *)&dac_ch_cfg[eDAC_VCEIN],	&p_dac_cfg[eDAC_VCEIN]},
		{eDAC_TOAREF,	ProcessDacValue,	(st_DAC_Chn *)&dac_ch_cfg[eDAC_TOAREF],	&p_dac_cfg[eDAC_TOAREF]},
		{eDAC_VANA2,	ProcessDacValue,	(st_DAC_Chn *)&dac_ch_cfg[eDAC_VANA2],	&p_dac_cfg[eDAC_VANA2]}
};

stADCChn *GetADC_Channels(void)
{
	return (stADCChn *)adc_ch_cfg;
}

stAnCfg* GetAnalog_cfg_dflt(void)
{
	return (stAnCfg*)an_cfg_dflt;
}

stAnCfg* GetAnalog_cfg(void)
{
	return p_ancfg;
}

stAnalogData *GetAnalogData(uint32_t channel)
{
	return (channel < eANCH_MAX) ? &an_values[channel] : NULL;
}

stDAC_Data *GetDACData(void)
{
	return dac_values;
}

stDAC_Cfg *GetDAC_Cfg_Dflt(void)
{
	return (stDAC_Cfg *)dac_cfg_dflt;
}

void DAC_Init(stDAC_Cfg *p_cfg)
{
	memcpy(p_dac_cfg, p_cfg, sizeof(p_dac_cfg));
	for (eDAC_CHANNELS x = 0; x < eDAC_MAX; x++)
	{
		Set_DAC_Value(x, p_dac_cfg[x].value);
		DAC_HandleTypeDef *V_nDAC = ((st_DAC_Chn *)&dac_ch_cfg[x])->nDAC;
		HAL_DAC_Start(V_nDAC, dac_values[x].p_chCfg->nChannel);
		while(V_nDAC->State == HAL_DAC_STATE_BUSY);
	}
}

void ADC_Init(stAnCfg *p_cfg)
{

	/* Default values initialization */
	for(uint32_t i = 0; i < eANCH_MAX; i++)
	{
		p_ancfg[i] = p_cfg[i];
		an_values[i].present_val = 0;
		an_values[i].sum_val = 0;
		an_values[i].mean_val = 0;
	}

	/* Internal band gap reference calibrated value at 30ºC */
	verfint_cal = (float)((VREFINT_CAL_VREF * (*VREFINT_CAL_ADDR)) / (ADC12_CNTS * 1000));
	an_values[eANCH_MCU_TEMP].mean_val = k_adc;

	/* MCU temperature sensor constants initialization */
	Init_MCU_Temp_Constants(an_values[eANCH_MCU_TEMP].p_cfg);

	HAL_ADC_Start_DMA(&hadc1, (uint32_t *)&adc_buf[0][0][0], ADC1_N_CH);
	HAL_ADC_Start_DMA(&hadc3, (uint32_t *)&adc_buf[1][0][0], ADC3_N_CH);
	HAL_ADC_Start_DMA(&hadc5, (uint32_t *)&adc_buf[2][0][0], ADC5_N_CH);
}

void Init_MCU_Temp_Constants(stAnCfg *p_cfg)
{
	/* MCU temperature sensor calibration parameters stored in ROM */

	float k_ts = ((float)((*TEMPSENSOR_CAL2_ADDR) - (*TEMPSENSOR_CAL1_ADDR))) / ((float)(TEMPSENSOR_CAL2_TEMP - TEMPSENSOR_CAL1_TEMP));
	float ts_vcal1 = ((float)(((*TEMPSENSOR_CAL1_ADDR) * TEMPSENSOR_CAL_VREFANALOG)) / ((float)(ADC12_CNTS * 1000)));

	p_cfg->AN.Slope = k_ts ;
	p_cfg->AN.Offset = ((float)TEMPSENSOR_CAL1_TEMP) - (k_ts * ts_vcal1);
}

void CollectAnalogData(void *p_val)
{
	stAnalogData *p_an = (stAnalogData *)p_val;
	//stAnCfg *p_cfg = p_an->p_cfg;
	//float x = ((float)*(p_an->p_chCfg->val)) * k_adc; //move this to the end as well
	//float y = p_an->present_val =  p_cfg->AN.Slope * x + p_cfg->AN.Offset; //move this to the end to speed up
	//p_an->mean_val = ((p_an->mean_val * (p_cfg->samples - 1.0)  + y ) / p_cfg->samples);
	//p_an->present_raw = *(p_an->p_chCfg->val);
	//p_an->sum_raw += p_an->present_raw;
	p_an->present_val = ((float) *(p_an->p_chCfg->val));
	p_an->sum_val += p_an->present_val; ///!!! DONT FORGET TO multiply k_adc and divide by analog_sample_count
}

void ProcessAnalogData(void *p_val)
{
	stAnalogData *p_an = (stAnalogData *)p_val;
	//stAnCfg *p_cfg = p_an->p_cfg;
	//float x = ((float)*(p_an->p_chCfg->val)) * k_adc; //move this to the end as well
	//p_an->mean_val =  (k_adc * p_cfg->AN.Slope) * p_an->mean_val + p_cfg->AN.Offset; //move this to the end to speed up
	//p_an->mean_val /= (k_adc * p_cfg->samples);
	p_an->mean_val = (p_an->sum_val * k_adc) / analog_sample_count;
	//p_an->mean_raw = p_an->present_raw;
}

void CollectAnalogVREFData(void *p_val)
{
	stAnalogData *p_an = p_val;
	//stAnCfg *p_cfg = p_an->p_cfg; //unused if not moving average
	p_an->present_val = ((float)*p_an->p_chCfg->val);
	//p_an->mean_val = ((p_an->mean_val * (p_cfg->samples - 1.0)  + p_an->present_val ) / p_cfg->samples);
	p_an->sum_val += p_an->present_val;
	/* Update ADC conversion constant */
	// k_adc = (p_an->mean_val / ADC16_CNTS);
}

void ProcessAnalogVREFData(void *p_val)
{
	stAnalogData *p_an = p_val;
	//stAnCfg *p_cfg = p_an->p_cfg; //unused if not moving average
	//p_an->mean_val = ((verfint_cal * ADC16_CNTS) / p_an->mean_val) / p_cfg->samples;
	p_an->mean_val = ((verfint_cal * ADC16_CNTS) / p_an->sum_val) / analog_sample_count;
	/* Update ADC conversion constant */
	// k_adc = (p_an->mean_val / ADC16_CNTS);
}

void CollectAnalogSensorData(void *p_val)
{
	return;
}

void ProcessAnalogSensorData(void *p_val)
{
	stAnalogData *p_an = (stAnalogData *)p_val;
	stAnCfg *p_cfg = p_an->p_cfg;
	float x = ((float)*(p_an->p_chCfg->val)) * k_adc;
	float y = p_an->present_val = p_cfg->AN.Slope * x + p_cfg->AN.Offset;
	p_an->mean_val = ((p_an->mean_val * (p_cfg->samples - 1.0)  + y ) / p_cfg->samples);
}



void ProcessDacValue(void *p_st)
{
	stDAC_Data *p_dac = p_st;
	//float x = p_dac->p_cfg->Slope * p_dac->set_val + p_dac->p_cfg->Offset;
	float x = p_dac->set_val; //In Volts
	HAL_DAC_SetValue(p_dac->p_chCfg->nDAC, p_dac->p_chCfg->nChannel, DAC_ALIGN_12B_R, lrintf(x * KDAC12));
	while(HAL_DAC_GetState(p_dac->p_chCfg->nDAC) != HAL_DAC_STATE_READY); //wait for completion
	//HAL_DAC_Start(p_dac->p_chCfg->nDAC, p_dac->p_chCfg->nChannel);
}

void Set_DAC_Value(eDAC_CHANNELS channel, float val)
{
	if(channel < eDAC_MAX)
	{
		dac_values[channel].set_val = val;
		dac_values[channel].dac_fnc(&dac_values[channel]);
	}
}

float Get_DAC_Value(eDAC_CHANNELS channel){
	return dac_values[channel].set_val;
}

void HAL_ADC_ConvCpltCallback(ADC_HandleTypeDef *hadc)
{
  /* Prevent unused argument(s) compilation warning */
  UNUSED(hadc);
  completed_ch_count++;
  /* NOTE : This function should not be modified. When the callback is needed,
            function HAL_ADC_ConvCpltCallback must be implemented in the user file.
   */
}


void Update_ADC_Readings(uint8_t *channels,uint8_t channel_cnt)
{
	/*This function reads requested number of (channel_cnt) ADC channels (*channels)
	also
	*/
	uint32_t i;
	uint16_t *p_wrk;
	uint8_t ch;
	//uint16_t *p_adc;

	//zero out each requested channel's sum
	for (i = 0; i < channel_cnt; i++){
		ch = channels[i];
		an_values[ch].sum_val = 0;
	}
	for (uint32_t n = 0; n < analog_sample_count; n++){
		//start reading each request channel
		completed_ch_count = 0; //count of completed channel readings
		for (i = 0; i < channel_cnt; i++){
			ch = channels[i];
			//p_adc = &adc_buf[ch][0][0];
			p_wrk = &adc_buf[ch][1][0];
			HAL_ADC_Start_DMA((ADC_HandleTypeDef *)p_adc_ins[ch], (uint32_t *)p_wrk, adc_ch_qty[ch]);
		}
		while(completed_ch_count < channel_cnt){} //wait until all ADC conversions are completed
		//collect read values
		for(i = 0; i < channel_cnt; i++){
			ch = channels[i];
			//while(p_adc_ins[ch]->DMA_Handle->State == HAL_DMA_STATE_BUSY){}
			//p_adc = &adc_buf[ch][0][0] , p_wrk = &adc_buf[ch][1][0];
			/* Move ADC conversion buffer to working buffer and start ADC capture */
			//memcpy(p_wrk, p_adc, sizeof(uint16_t) * adc_ch_qty[i]);
			//an_values[ch].p_chCfg->nADC == p_adc_ins[ch];
			an_values[ch].collect_fnc(&an_values[ch]);
		}
	}
	//now process them
	for(i = 0; i < channel_cnt; i++){
		ch = channels[i];
		an_values[ch].process_fnc(&an_values[ch]);
	}
	//The range-switching that used to follow here (before firmware 1.5) had
	//one-sided thresholds on this two-sided signal, so it never moved to a
	//more sensitive range and stepped the wrong way for large anodic
	//currents. Ranges now change only in batches, between steps.
}

float Read_WEOUT_Fast(void)
{
	/* One conversion of the current channel without the ten-sample average:
	 * tens of microseconds, quick enough to catch the charging spike right
	 * after a batch step for the auto-range decision. Returns the ADC volts,
	 * or -1 if the converter does not finish within 1 ms. */
	uint16_t *p_src = &adc_buf[eANCH_WEOUT][1][0];
	CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
	DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
	uint32_t cap_cyc = (SystemCoreClock / 1000000U) * 1000U;
	completed_ch_count = 0;
	HAL_ADC_Start_DMA((ADC_HandleTypeDef *)p_adc_ins[eANCH_WEOUT],
			(uint32_t *)p_src, adc_ch_qty[eANCH_WEOUT]);
	uint32_t t0 = DWT->CYCCNT;
	while (completed_ch_count < 1) {
		if ((DWT->CYCCNT - t0) > cap_cyc) return -1.0f;
	}
	return (float)(*p_src) * k_adc;
}

/*! \fn float Set_TIA_Gain(eTIA_GAIN gain)


	\brief Set trans-impedande amplifier gain.

	\param[in] 	gain, allowed values are in type enum eTIA_GAIN
	\return		real gain value in float
 */

static const tia_switch tia_switch_matrix[] = {
		{SW_GAIN_93R, TIA_GAIN_93R},
		{SW_GAIN_1K, TIA_GAIN_1K},
		{SW_GAIN_10K, TIA_GAIN_10K},
		{SW_GAIN_91K, TIA_GAIN_91K},
		{SW_GAIN_1M, TIA_GAIN_1M}
};

float TIA_Gain_Ohms(eTIA_GAIN gain)
{
	return tia_switch_matrix[CLAMP(gain, eTIA_SW_GAIN_100, eTIA_SW_GAIN_1M)].f_gain;
}

float Set_TIA_Gain(eTIA_GAIN gain)
{
	uint32_t val = LL_GPIO_ReadOutputPort(GPIOB);	// Read port present value
	uint32_t ix = CLAMP(gain, eTIA_SW_GAIN_100, eTIA_SW_GAIN_1M);

	val &= ~TIA_SW_MASK;	// Clear TIA gain switch bits
	val |= (tia_switch_matrix[ix].value &  TIA_SW_MASK);	// Set TIA switch gain bits

	LL_GPIO_WriteOutputPort(GPIOB, val);

	return tia_switch_matrix[ix].f_gain;
}

void Pstat_RunTime(void *argument)
{
	uint32_t now_ms;
	uint32_t last_blink_ms = HAL_GetTick() - 1000;
	while(1)
	{
		now_ms = HAL_GetTick();
		ProcessDacWriteBatchStep();
		CurrentHoldStep();
		//Run the received command once its frame is complete
		Serial_Poll();
		//osDelay(1);
		if ((now_ms - last_blink_ms) >= 1000){
			last_blink_ms = now_ms; //was never updated before, so this branch ran every pass
			for(uint32_t x = 0; x < LEDS_NUMBER; x++){
				BlinkLed_Compute(&LED_List[x]);
			}
		}
		//Feed the watchdog once per loop pass (the long-standing de facto
		//behavior, kept deliberately). If this loop ever freezes, the
		//independent watchdog resets the chip after ~2 s and the output
		//switch returns to its power-on (off) state — the fail-safe of
		//last resort for a wedged instrument.
		HAL_IWDG_Refresh(&hiwdg);
	}
}

static uint32_t Capture_Around_Step(uint32_t n_samples, uint32_t period_us,
		uint32_t n_pre, uint8_t by_dac, float dac_v, uint8_t *out_buf,
		uint32_t *actual_duration_us, uint32_t *centre_ns)
{
	/* Record WE_OUT raw ADC counts at a fixed pace, timed by the CPU cycle
	 * counter, and make a potential step as sample n_pre is due: either by
	 * closing the CE switch onto a DAC already parked at the step potential,
	 * or by moving the DAC with the cell already connected. Used for the R_u
	 * measurement: in the instant after the step the cell behaves like a
	 * plain resistor, and this loop samples within microseconds of that
	 * instant, where a reading over the serial link lands one USB round trip
	 * (~3 ms) later, past most of the transient.
	 *
	 * Raw oversampled conversions, deliberately: no averaging across
	 * samples, no auto-gain, no calibration. The host converts counts with
	 * its own calibration, and the gain is whatever the host set before the
	 * capture.
	 *
	 * On success the switch is LEFT CLOSED so the host can do its
	 * spike-free disconnect; a wedged ADC conversion opens it here (raw,
	 * fail-safe) and returns the samples captured so far. When centre_ns is
	 * given it receives the time from the step to the middle of the first
	 * conversion after it. */
	uint16_t *out = (uint16_t *)out_buf;
	uint16_t *p_src = &adc_buf[eANCH_WEOUT][1][0];
	uint32_t captured = 0;

	/* The cycle counter is free-running only while enabled; enabling is
	 * idempotent and survives being already on under a debugger. */
	CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
	DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;

	uint32_t cyc_per_us = SystemCoreClock / 1000000U;
	uint32_t period_cyc = period_us * cyc_per_us;
	uint32_t conv_cap_cyc = 2000U * cyc_per_us; /* a conversion takes ~us; 2 ms means wedged */
	uint32_t step_cyc = 0;

	uint32_t t0 = DWT->CYCCNT;
	for (uint32_t i = 0; i < n_samples; i++){
		uint32_t due = t0 + (i * period_cyc);
		while ((int32_t)(DWT->CYCCNT - due) < 0) {}
		if (i == n_pre) {
			step_cyc = DWT->CYCCNT;
			if (by_dac) {
				Set_DAC_Value(eDAC_VCEIN, dac_v);
			} else {
				HAL_GPIO_WritePin(CE_EN_GPIO_Port, CE_EN_Pin, GPIO_PIN_SET);
				switch_val = eSWITCH_ON;
			}
		}
		completed_ch_count = 0;
		HAL_ADC_Start_DMA((ADC_HandleTypeDef *)p_adc_ins[eANCH_WEOUT],
				(uint32_t *)p_src, adc_ch_qty[eANCH_WEOUT]);
		uint32_t conv_t0 = DWT->CYCCNT;
		while (*(volatile uint8_t *)&completed_ch_count < 1) {
			if ((DWT->CYCCNT - conv_t0) > conv_cap_cyc) {
				HAL_GPIO_WritePin(CE_EN_GPIO_Port, CE_EN_Pin, GPIO_PIN_RESET);
				switch_val = eSWITCH_OFF;
				*actual_duration_us = (DWT->CYCCNT - t0) / cyc_per_us;
				return captured;
			}
		}
		if (i == n_pre && centre_ns != NULL) {
			uint32_t done = DWT->CYCCNT;
			*centre_ns = (uint32_t)(((uint64_t)(conv_t0 - step_cyc)
					+ (uint64_t)(done - step_cyc)) * 500U / cyc_per_us);
		}
		out[captured++] = *p_src;
		/* Deliberate foreground work, not a hang: keep the watchdog fed the
		 * way the main loop does. The bounded conversion wait above keeps a
		 * genuine wedge inside the watchdog's reach. */
		HAL_IWDG_Refresh(&hiwdg);
	}
	*actual_duration_us = (DWT->CYCCNT - t0) / cyc_per_us;
	return captured;
}

uint32_t StepCapture(uint32_t n_samples, uint32_t period_us,
                     uint8_t *out_buf, uint32_t *actual_duration_us)
{
	/* The step is the CE switch closing, at the first sample. While the
	 * switch was open the control amplifier wound up against its rail, so
	 * the first ~50 us of the trace are its recovery, not the cell. */
	return Capture_Around_Step(n_samples, period_us, 0, 0, 0.0f, out_buf,
	                           actual_duration_us, NULL);
}

uint32_t DacStepCapture(uint32_t n_samples, uint32_t period_us, uint32_t n_pre,
                        float dac_v, uint8_t *out_buf,
                        uint32_t *actual_duration_us, uint32_t *centre_ns)
{
	/* The step is the DAC moving to dac_v at sample n_pre, with the cell
	 * already connected and the loop in control, so the trace holds only
	 * the cell's response; the n_pre samples before it give its zero. */
	return Capture_Around_Step(n_samples, period_us, n_pre, 1, dac_v,
	                           out_buf, actual_duration_us, centre_ns);
}

int32_t CurrentHoldStep(){
	if (current_hold_active != 1) {return 0;}
	const float current_step_max = 0.05;
	const float current_step_min = (3.3/4095);
	const float smallest_V_step = (3.3 / 65535);
	uint8_t ch_list[] = {eANCH_WEOUT};
	uint8_t ch_cnt = 1;
	float set_V = dac_values[eDAC_VCEIN].set_val;
	//old value is required to calculate the next step size
	old_current_V = an_values[eANCH_WEOUT].mean_val;
	Update_ADC_Readings(ch_list, ch_cnt);
	//residual current in the unit of V reading from the ADC WEOUT channel
	float residual_current_V = target_current_V - an_values[eANCH_WEOUT].mean_val;
	if (fabs(residual_current_V) <= smallest_V_step){return 0;}
	float delta_current_V = fabs(old_current_V - an_values[eANCH_WEOUT].mean_val);
	//limit how small delta can be (prevent noise from messing the step size)
	if ((delta_current_V) < (0.33 / 65535)) {delta_current_V = (0.33 / 65535);}
	//estimate the next best step based on the previous current change vs the amount of V
	current_hold_step_V *= (current_hold_adj_rate * (residual_current_V / delta_current_V));
	current_hold_step_V = CLAMP(fabs(current_hold_step_V), current_step_min, current_step_max);
	//prevent noisy result from returning wrong sign of movement
	//always set to correct direction
	if (residual_current_V > 0) {
		current_hold_step_V = -1 * current_hold_step_V;
	} else {
		current_hold_step_V = current_hold_step_V;
	}
	set_V += current_hold_step_V;
	set_V = CLAMP(set_V, 0.0, 3.3);
	Set_DAC_Value(eDAC_VCEIN, set_V);
	//DAC_HandleTypeDef *V_nDAC = ((st_DAC_Chn *)&dac_ch_cfg[eDAC_VCEIN])->nDAC;
	//wait for DAC to finish its job
	//while(V_nDAC->State == HAL_DAC_STATE_BUSY){}
	return 1;
}


