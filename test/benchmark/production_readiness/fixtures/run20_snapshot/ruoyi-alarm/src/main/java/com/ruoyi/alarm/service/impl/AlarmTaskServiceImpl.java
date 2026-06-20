package com.ruoyi.alarm.service.impl;

import java.util.Date;
import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ruoyi.alarm.domain.AlarmTask;
import com.ruoyi.alarm.domain.AlarmTaskChannel;
import com.ruoyi.alarm.domain.dto.AlarmTaskTestDTO;
import com.ruoyi.alarm.mapper.AlarmTaskChannelMapper;
import com.ruoyi.alarm.mapper.AlarmTaskMapper;
import com.ruoyi.alarm.service.IAlarmTaskService;
import com.ruoyi.common.utils.DateUtils;
import com.ruoyi.common.utils.ShiroUtils;
import com.ruoyi.common.utils.StringUtils;

/**
 * 预警任务 服务实现
 *
 * @author ruoyi
 */
@Service
public class AlarmTaskServiceImpl implements IAlarmTaskService
{
    @Autowired
    private AlarmTaskMapper alarmTaskMapper;

    @Autowired
    private AlarmTaskChannelMapper alarmTaskChannelMapper;

    /**
     * 查询预警任务
     *
     * @param taskId 预警任务ID
     * @return 预警任务
     */
    @Override
    public AlarmTask selectAlarmTaskById(Long taskId)
    {
        AlarmTask alarmTask = alarmTaskMapper.selectAlarmTaskById(taskId);
        if (alarmTask != null)
        {
            alarmTask.setChannels(alarmTaskChannelMapper.selectAlarmTaskChannelsByTaskId(taskId));
        }
        return alarmTask;
    }

    /**
     * 根据任务名称查询预警任务
     *
     * @param taskName 任务名称
     * @return 预警任务
     */
    @Override
    public AlarmTask selectAlarmTaskByName(String taskName)
    {
        return alarmTaskMapper.selectAlarmTaskByName(taskName);
    }

    /**
     * 查询预警任务列表
     *
     * @param alarmTask 预警任务
     * @return 预警任务集合
     */
    @Override
    public List<AlarmTask> selectAlarmTaskList(AlarmTask alarmTask)
    {
        return alarmTaskMapper.selectAlarmTaskList(alarmTask);
    }

    /**
     * 新增预警任务
     *
     * @param alarmTask 预警任务
     * @return 结果
     */
    @Override
    @Transactional
    public int insertAlarmTask(AlarmTask alarmTask)
    {
        alarmTask.setCreateTime(DateUtils.getNowDate());
        alarmTask.setCreateBy(ShiroUtils.getSysUser().getLoginName());
        if (StringUtils.isEmpty(alarmTask.getStatus()))
        {
            alarmTask.setStatus("0");
        }
        return alarmTaskMapper.insertAlarmTask(alarmTask);
    }

    /**
     * 修改预警任务
     *
     * @param alarmTask 预警任务
     * @return 结果
     */
    @Override
    @Transactional
    public int updateAlarmTask(AlarmTask alarmTask)
    {
        alarmTask.setUpdateTime(DateUtils.getNowDate());
        alarmTask.setUpdateBy(ShiroUtils.getSysUser().getLoginName());
        return alarmTaskMapper.updateAlarmTask(alarmTask);
    }

    /**
     * 批量删除预警任务
     *
     * @param taskIds 需要删除的预警任务ID
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteAlarmTaskByIds(Long[] taskIds)
    {
        for (Long taskId : taskIds)
        {
            alarmTaskChannelMapper.deleteAlarmTaskChannelByTaskId(taskId);
        }
        return alarmTaskMapper.deleteAlarmTaskByIds(taskIds);
    }

    /**
     * 删除预警任务
     *
     * @param taskId 预警任务ID
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteAlarmTaskById(Long taskId)
    {
        alarmTaskChannelMapper.deleteAlarmTaskChannelByTaskId(taskId);
        return alarmTaskMapper.deleteAlarmTaskById(taskId);
    }

    /**
     * 获取任务绑定的渠道配置列表
     *
     * @param taskId 任务ID
     * @return 渠道配置列表
     */
    @Override
    public List<AlarmTaskChannel> selectAlarmTaskChannels(Long taskId)
    {
        return alarmTaskChannelMapper.selectAlarmTaskChannelsByTaskId(taskId);
    }

    /**
     * 测试发送预警
     *
     * @param testDTO 测试参数
     * @return 结果
     */
    @Override
    public boolean testSend(AlarmTaskTestDTO testDTO)
    {
        // TODO: 实现测试发送逻辑
        return true;
    }
}
