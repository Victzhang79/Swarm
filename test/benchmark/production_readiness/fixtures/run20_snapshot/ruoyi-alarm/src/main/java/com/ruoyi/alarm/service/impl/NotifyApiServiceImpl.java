package com.ruoyi.alarm.service.impl;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import com.ruoyi.alarm.domain.AlarmTask;
import com.ruoyi.alarm.domain.dto.NotifyRequestDTO;
import com.ruoyi.alarm.service.INotifyApiService;
import com.ruoyi.alarm.service.IAlarmAppService;
import com.ruoyi.alarm.service.IAlarmTaskService;
import com.ruoyi.alarm.service.IAlarmEngineService;
import com.ruoyi.common.utils.StringUtils;

/**
 * 对外预警API服务实现
 * 负责参数校验、任务定位、幂等收敛、调用引擎处理
 * 
 * @author ruoyi
 */
@Service
public class NotifyApiServiceImpl implements INotifyApiService
{
    @Autowired
    private IAlarmAppService alarmAppService;

    @Autowired
    private IAlarmTaskService alarmTaskService;

    @Autowired
    private IAlarmEngineService alarmEngineService;

    @Override
    public NotifyRequestDTO.NotifyResponse processSimpleNotify(NotifyRequestDTO.NotifySimpleRequest request)
    {
        // 校验应用凭证
        if (!alarmAppService.validateApp(request.getAppId(), request.getAppSecret()))
        {
            return NotifyRequestDTO.NotifyResponse.error(401, "应用凭证校验失败");
        }

        // 校验任务名称
        if (StringUtils.isEmpty(request.getTaskName()))
        {
            return NotifyRequestDTO.NotifyResponse.error(400, "任务名称不能为空");
        }

        // 根据任务名称查找任务
        AlarmTask task = alarmTaskService.selectAlarmTaskByName(request.getTaskName());
        if (task == null)
        {
            return NotifyRequestDTO.NotifyResponse.error(404, "预警任务不存在");
        }

        // 调用引擎处理
        return alarmEngineService.processSimpleNotify(request);
    }

    @Override
    public NotifyRequestDTO.NotifyResponse processComposeNotify(NotifyRequestDTO.NotifyComposeRequest request)
    {
        // 校验应用凭证
        if (!alarmAppService.validateApp(request.getAppId(), request.getAppSecret()))
        {
            return NotifyRequestDTO.NotifyResponse.error(401, "应用凭证校验失败");
        }

        // 校验任务名称
        if (StringUtils.isEmpty(request.getTaskName()))
        {
            return NotifyRequestDTO.NotifyResponse.error(400, "任务名称不能为空");
        }

        // 根据任务名称查找任务
        AlarmTask task = alarmTaskService.selectAlarmTaskByName(request.getTaskName());
        if (task == null)
        {
            return NotifyRequestDTO.NotifyResponse.error(404, "预警任务不存在");
        }

        // 调用引擎处理
        return alarmEngineService.processComposeNotify(request);
    }

    @Override
    public NotifyRequestDTO.NotifyResponse processRecover(NotifyRequestDTO.NotifyRecoverRequest request)
    {
        // 校验应用凭证
        if (!alarmAppService.validateApp(request.getAppId(), request.getAppSecret()))
        {
            return NotifyRequestDTO.NotifyResponse.error(401, "应用凭证校验失败");
        }

        // 校验任务名称
        if (StringUtils.isEmpty(request.getTaskName()))
        {
            return NotifyRequestDTO.NotifyResponse.error(400, "任务名称不能为空");
        }

        // 根据任务名称查找任务
        AlarmTask task = alarmTaskService.selectAlarmTaskByName(request.getTaskName());
        if (task == null)
        {
            return NotifyRequestDTO.NotifyResponse.error(404, "预警任务不存在");
        }

        // 调用引擎处理
        return alarmEngineService.processRecover(request);
    }

    @Override
    public NotifyRequestDTO.NotifyResponse processDelete(NotifyRequestDTO.NotifyDeleteRequest request)
    {
        // 校验应用凭证
        if (!alarmAppService.validateApp(request.getAppId(), request.getAppSecret()))
        {
            return NotifyRequestDTO.NotifyResponse.error(401, "应用凭证校验失败");
        }

        // 校验任务名称
        if (StringUtils.isEmpty(request.getTaskName()))
        {
            return NotifyRequestDTO.NotifyResponse.error(400, "任务名称不能为空");
        }

        // 根据任务名称查找任务
        AlarmTask task = alarmTaskService.selectAlarmTaskByName(request.getTaskName());
        if (task == null)
        {
            return NotifyRequestDTO.NotifyResponse.error(404, "预警任务不存在");
        }

        // 调用引擎处理
        return alarmEngineService.processDelete(request);
    }

    @Override
    public NotifyRequestDTO.NotifyResponse processVoipNotify(NotifyRequestDTO.VoipSendRequest request)
    {
        // 校验应用凭证
        if (!alarmAppService.validateApp(request.getAppId(), request.getAppSecret()))
        {
            return NotifyRequestDTO.NotifyResponse.error(401, "应用凭证校验失败");
        }

        // 校验任务名称
        if (StringUtils.isEmpty(request.getTaskName()))
        {
            return NotifyRequestDTO.NotifyResponse.error(400, "任务名称不能为空");
        }

        // 根据任务名称查找任务
        AlarmTask task = alarmTaskService.selectAlarmTaskByName(request.getTaskName());
        if (task == null)
        {
            return NotifyRequestDTO.NotifyResponse.error(404, "预警任务不存在");
        }

        // 调用引擎处理
        return alarmEngineService.processVoipNotify(request);
    }

    @Override
    public NotifyRequestDTO.NotifyResponse processCallback(NotifyRequestDTO.CallbackRequest request)
    {
        // 校验参数
        if (request.getRecordId() == null)
        {
            return NotifyRequestDTO.NotifyResponse.error(400, "记录ID不能为空");
        }

        // 调用引擎处理
        alarmEngineService.processCallback(request.getRecordId(), request.getCallbackType());
        return NotifyRequestDTO.NotifyResponse.success();
    }
}
