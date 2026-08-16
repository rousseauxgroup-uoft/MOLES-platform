/*
 * Analog.h
 *
 *  Created on: 22 nov. 2017
 *      Author: agarcia
 */

#ifndef ANALOG_H_
#define ANALOG_H_

#define TIA_SW_PORT 	GPIOB
#define TIA_SW_SHIFT	9
#define TIA_SW_MASK	 	(0xf << TIA_SW_SHIFT)
#define SW_GAIN_1K		(0x07 << TIA_SW_SHIFT)
#define SW_GAIN_10K		(0x0b << TIA_SW_SHIFT)
#define SW_GAIN_100K	(0x0d << TIA_SW_SHIFT)
#define SW_GAIN_1M		(0x0e << TIA_SW_SHIFT)
#define SW_GAIN_10M		(0x0f << TIA_SW_SHIFT)

/* Gain for trans-impedance amplifier is 10M fixed resistor
 * in parallel with switched resistors : 1K, 10K, 100K, 1M */


#define TIA_GAIN_1K		999.900009
#define TIA_GAIN_10K	9990.00999
#define TIA_GAIN_100K	99009.90099
#define TIA_GAIN_1M		909090.9091
#define TIA_GAIN_10M	1e7

#define CONVERT_TIMEOUT	100

#define ADCVREF		3.3

#define ADC12_CNTS	4095.0
#define KADC12		(ADCVREF / ADC12_CNTS)

#define ADC16_CNTS	65535.0
#define KADC16		(ADCVREF / ADC16_CNTS)

#define KDAC12		(1 / KADC12)

/* Number of channels to sample for each ADC */

#define ADC1_N_CH	1
#define ADC3_N_CH	1
#define ADC5_N_CH	3

/* ADC channel enumerations */

enum eADC1_CHANNELS
{
	eADC1_WEOUT = ADC_CHANNEL_2,
};

enum eADC3_CHANNELS
{
	eADC3_REOUT = ADC_CHANNEL_12,
};

enum eADC5_CHANNELS
{
	eADC5_VREFINT = ADC_CHANNEL_VREFINT,
	eADC5_MCU_TEMP = ADC_CHANNEL_TEMPSENSOR_ADC5,
	eADC5_MCU_VBAT = ADC_CHANNEL_VBAT,
};

/* Analog channel enumeration such as it will
 * be arranged in the configuration array.*/

typedef enum _eAN_CHANNELS
{
	eANCH_WEOUT = 0,
	eANCH_REOUT,
	eANCH_VREFINT,
	eANCH_MCU_TEMP,
	eANCH_MCU_VBAT,
	eANCH_MAX
}eAN_CHANNELS;

/* Analog input structure definitions */

typedef struct tag_st_ADCChn
{
	void *nADC;				// Basse address of ADC module
	uint32_t nChannel;		// Number of channel
	uint16_t *val;			// pointer to raw adc value
}stADCChn;

/* Analog input scale, offset and mean configuration structure */
typedef struct tag_stAnCfg
{
	struct
	{
		float Slope;
		float Offset;
	}AN;
	float samples;
}stAnCfg;

/* Analog input working data structure */
typedef struct tag_stAnalogData
{
	eAN_CHANNELS id;
	const void (*collect_fnc)(void *); //function to collect samples' data
	const void (*process_fnc)(void *);// function to process collected data
	const stADCChn *p_chCfg;				// pointer to ADC channel configuration
	stAnCfg *p_cfg;							// Pointer to analog configuration struct
	float present_val;						// Raw instant value
	float mean_val;							// Processed averaged value
	float sum_val;							// Sum of raw instant values
//	uint32_t present_raw;
//	uint32_t sum_raw;
//	uint32_t mean_raw;
}stAnalogData;

/* Comparator's negative input DAC structure definitions */

typedef enum _eDAC_CHANNELS
{
	eDAC_VCEIN = 0,
	eDAC_TOAREF = 1,
	eDAC_VANA2 = 2,
	eDAC_MAX = 3
}eDAC_CHANNELS;

/* DAC channel structure definition */

typedef struct tag_st_DAC_Chn
{
	void *nDAC;				// Base address of ADC module
	uint32_t nChannel;		// Number of channel
}st_DAC_Chn;

/* DAC scale, offset and mean configuration structure */
typedef struct tag_stDAC_Cfg
{
	float Slope;
	float Offset;
	float value;	// Initial DAC value
}stDAC_Cfg;

/* DAC working data structure */
typedef struct tag_stDAC_Data
{
	eAN_CHANNELS id;
	const void (*dac_fnc)(void *);	// function to process data
	const st_DAC_Chn *p_chCfg;				// pointer to DAC configuration struct
	stDAC_Cfg *p_cfg;						// Pointer to analog configuration struct
	float set_val;						// Present set value
}stDAC_Data;

typedef enum _eTIA_SW_GAIN
{
	eTIA_SW_GAIN_100 = 0,
	eTIA_SW_GAIN_1K = 1,
	eTIA_SW_GAIN_10K = 2,
	eTIA_SW_GAIN_100K =3,
	eTIA_SW_GAIN_1M = 4,
//	eTIA_SW_GAIN_10M = 5
}eTIA_GAIN;

typedef enum _eSWITCH_STS
{
	eSWITCH_OFF = 0,
	eSWITCH_ON = 1,
} eSWITCH_STS;

typedef struct _tia_switch
{
	uint32_t value;
	float f_gain;
}tia_switch;

stADCChn *GetADC_Channels(void);
stAnCfg* GetAnalog_cfg_dflt(void);
stAnCfg* GetAnalog_cfg(void);
stAnalogData *GetAnalogData(uint32_t);
stDAC_Cfg *GetDAC_Cfg_Dflt(void);
stDAC_Data *GetDACData(void);
void DAC_Init(stDAC_Cfg *p_cfg);
void ADC_Init(stAnCfg *p_cfg);
void Init_MCU_Temp_Constants(stAnCfg *p_cfg);
void CollectAnalogData(void *p_val);
void ProcessAnalogData(void *p_val);
void CollectAnalogVREFData(void *p_val);
void ProcessAnalogVREFData(void *p_val);
void CollectAnalogSensorData(void *p_val);
void ProcessAnalogSensorData(void *p_val);
void ProcessDacValue(void *p_st);
void Set_DAC_Value(eDAC_CHANNELS channel, float val);
float Get_DAC_Value(eDAC_CHANNELS channel);
void Update_ADC_Readings(uint8_t *channels,uint8_t channel_cnt);
float Set_TIA_Gain(eTIA_GAIN gain);
void Pstat_RunTime(void *argument);
int32_t CurrentHoldStep();
uint32_t StepCapture(uint32_t n_samples, uint32_t period_us,
                     uint8_t *out_buf, uint32_t *actual_duration_us);

const extern st_DAC_Chn dac_ch_cfg[];

extern float target_current_V;
extern uint8_t current_hold_active;
extern float current_hold_step_V;
extern float old_current_V;
extern float old_set_V;
extern float current_hold_adj_rate;
extern uint32_t analog_sample_count;
extern uint8_t auto_gain;

#endif /* ANALOG_H_ */
